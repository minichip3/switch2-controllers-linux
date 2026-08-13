"""Bridge: connect to one or more Switch 2 controllers over raw L2CAP and feed a
uinput virtual gamepad each, with automatic reconnection. Pure userspace; no
BlueZ GATT, no kernel modules.

Connection uses a central BLE scanner: when a saved pad advertises (button press
or Sync), we stop scanning and dial L2CAP immediately. Raw connect cannot run
while the adapter is discovering — including Steam's background scan — so scan
bursts are kept short and always stopped before connect.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from bleak import BleakScanner
from bleak.exc import BleakDBusError

from . import att
from . import protocol as P
from .config import CONFIG_DIR, Config, ControllerEntry
from .device import SwitchController
from .dsu import DSUServer
from .gamepad import JOYCON2_PAIR_BUTTON_MAP, JOYCON2_PAIR_PID, SwitchGamepad
from .motion_evdev import MotionEvdev
from .status import BridgeState, ControllerState, clear_state, write_state

# Written by system/bazzite-set-player-leds.py when emulator player order changes.
_LED_PLAYERS_PATH = CONFIG_DIR / "led-players.json"


def _stick_to_dsu(value: float) -> int:
    """Map a calibrated -1.0..1.0 axis to DSU's 0..255 range (128 neutral)."""
    return max(0, min(255, int(round(128 + value * 127))))

logger = logging.getLogger(__name__)

# Most adapters allow only ONE outstanding LE create-connection at a time.
_CONNECT_LOCK = threading.Lock()
_STATUS_INTERVAL_S = 1.5
# Delay between our own BLE scan burst stopping and the first btmgmt/connect
# call afterward. Real-hardware finding: sending an HCI/mgmt command while
# our own prior radio activity (a pending connect, or -- this one -- our
# own scan) hasn't fully settled stalls behind it on the same HCI command
# queue for a real, measured ~1.5s (see _connect_dst_with_polling's doc
# comment for the pending-connect case). The old 0.10s value predates that
# finding and was too short to matter -- consistent with the connect right
# after a scan burst failing almost every time on real hardware, succeeding
# only once enough time had passed some other way (e.g. a later scan-loop
# pass). 1.5s matches the measured settle cost directly instead of guessing
# at a smaller number.
_SCAN_SETTLE_S = 1.5
# Time budget to poll ONE pending connect (one dst_type within one outer
# attempt) before giving up on it -- see _connect_dst_with_polling, whose
# doc comment covers the debug-instrumented real-hardware finding that
# re-suppressing BR/EDR Inquiry *during* this wait costs more than it
# saves (a btmgmt call issued while our own LE Create Connection is
# pending stalls on the same HCI command queue as that pending connect).
# Suppressed once before the connect starts and then left alone.
_CONNECT_ATTEMPT_S = 3.0
# Tried a shorter budget for "wake" mode specifically here, on the theory
# its advertising window was tighter than "pairing" mode's -- reverted,
# 미니 confirmed the two modes' advertising windows are both ~10s, so that
# wasn't the actual difference. The real pattern (per real-hardware
# testing): the *first* connect attempt right after our own scan burst
# fails almost every time, on both modes, and only a later pass (after our
# own scan has had more time to settle) succeeds -- see _SCAN_SETTLE_S.
# Each dst_type tried costs ~1.5s (HCI-queue tax) + up to _CONNECT_ATTEMPT_S
# regardless of outcome (see _connect_dst_with_polling), and both types are
# always tried (see _connect_sync -- skipping the non-hinted type turned
# out to be unsafe), so one outer attempt already costs ~9s -- close to
# the Joy-Con 2's ~10s wake-mode advertising window on its own. 1 outer
# attempt per _connect_sync call; the scan loop retries on the pad's next
# advertisement regardless, so this just means a failed pass waits for
# that next advertisement instead of burning through a second one here
# that likely wouldn't fit in the same window anyway.
_CONNECT_ATTEMPTS = 1


def _adapter_index() -> str:
    """Prefer hci0; allow override via NGC_HCI (e.g. '1')."""
    return os.environ.get("NGC_HCI", "0").strip() or "0"


_BTMGMT_LOCK = threading.Lock()
_LAST_BT_INQUIRY_OFF = 0.0
_LAST_BT_LE_SCAN_OFF = 0.0
_BT_INQUIRY_OFF_MIN_INTERVAL_S = 0.35

# Consecutive failed connect passes for a pad -- whether or not it has ever
# connected in this process -- before we power-cycle the adapter (see
# _power_cycle_adapter). Each pass is _CONNECT_ATTEMPTS x _CONNECT_ATTEMPT_S
# ~ 7s, so this fires after ~15s of continuous failure, matching the "only
# a power cycle clears it" symptom observed on real hardware.
_RECONNECT_FAILURES_BEFORE_POWER_CYCLE = 2


def _run_quiet(cmd: list[str], *, timeout: float = 2.0) -> None:
    """Best-effort subprocess; never raises into the bridge."""
    try:
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        pass


def _force_bt_inquiry_off(*, force: bool = False) -> None:
    """HCI-level BR/EDR Inquiry stop.

    Real-hardware root cause (same host, sibling joyfusion project's btmon
    investigation): the interference that blocks raw L2CAP connect here was
    never an *LE* scan despite this function's old name and old ``-l`` flag
    -- bluetoothd (or something upstream; every external client candidate
    was ruled out there) restarts ordinary BR/EDR ``Inquiry`` back-to-back,
    a fresh one firing within ~1s of the previous ``Inquiry Complete``.
    ``btmgmt stop-find -l`` against BR/EDR discovery returns "Invalid
    Parameters" instantly (a type mismatch), which is exactly why the old
    flag silently never helped. ``-b`` actually stops it (``Discovering``
    flips to ``no``), just not for long, so callers keep re-calling this
    around connect attempts (see _connect_sync).

    BlueZ's D-Bus ``StopDiscovery`` only ends *our* session; ``btmgmt``
    clears the HCI-level discovery regardless of who started it. Requires
    passwordless ``sudo`` for btmgmt (Bazzite default for this user).

    Never raises — a hung btmgmt must not crash the bridge.
    """
    global _LAST_BT_INQUIRY_OFF
    now = time.monotonic()
    with _BTMGMT_LOCK:
        if not force and (now - _LAST_BT_INQUIRY_OFF) < _BT_INQUIRY_OFF_MIN_INTERVAL_S:
            return
        _LAST_BT_INQUIRY_OFF = now
        idx = _adapter_index()
        # Start detached-ish: kill hung btmgmt so we never block the hub.
        try:
            proc = subprocess.Popen(
                ["sudo", "-n", "btmgmt", "-i", idx, "stop-find", "-b"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=0.5)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass


def _force_bt_le_scan_off(*, force: bool = False) -> None:
    """HCI-level LE scan stop (btmgmt stop-find -l).

    D-Bus StopDiscovery only ends *our* session, so when another client
    (Steam Input, decky-bluetooth-wake-control) holds the adapter's LE
    discovery and our scanner.start() gets [org.bluez.Error.InProgress],
    btmgmt is the only way to clear it at the HCI level. Uses its own
    throttle so it is not skipped when called right after _force_bt_inquiry_off.
    Never raises.
    """
    global _LAST_BT_LE_SCAN_OFF
    now = time.monotonic()
    with _BTMGMT_LOCK:
        if not force and (now - _LAST_BT_LE_SCAN_OFF) < _BT_INQUIRY_OFF_MIN_INTERVAL_S:
            return
        _LAST_BT_LE_SCAN_OFF = now
        idx = _adapter_index()
        try:
            proc = subprocess.Popen(
                ["sudo", "-n", "btmgmt", "-i", idx, "stop-find", "-l"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=0.5)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass


def _power_cycle_adapter() -> None:
    """Toggle adapter power to clear a wedged radio state.

    Real-hardware finding (today): once reconnects start failing, "only a
    Bluetooth power cycle recovers" the adapter -- no amount of
    stop-find/settle retries gets a connect through. This automates that
    manual recovery. Uses BlueZ D-Bus power toggle (no sudo needed) and
    drops other Bluetooth devices for a moment; only called by the hub when
    every previously-connected pad has failed to reconnect repeatedly, so
    there's no live session to lose.

    Never raises.
    """
    _run_quiet(["bluetoothctl", "power", "off"], timeout=3.0)
    time.sleep(1.0)
    _run_quiet(["bluetoothctl", "power", "on"], timeout=3.0)
    time.sleep(2.0)


def _bluez_remove_device(mac: str) -> None:
    """Drop BlueZ's Device object so it can't race our raw ATT connect.

    Do not call this while *we* already own a live session for ``mac`` — BlueZ
    treating the ACL as its own Connected device means RemoveDevice tears the
    link down.
    """
    if not mac:
        return
    _run_quiet(["bluetoothctl", "remove", mac], timeout=2.0)
    path = f"/org/bluez/hci{_adapter_index()}/dev_{mac.upper().replace(':', '_')}"
    _run_quiet(
        ["busctl", "call", "org.bluez", f"/org/bluez/hci{_adapter_index()}",
         "org.bluez.Adapter1", "RemoveDevice", "o", path],
        timeout=1.5,
    )


def _is_discovering() -> bool:
    """Check whether BlueZ discovery is currently active."""
    try:
        proc = subprocess.run(
            ["bluetoothctl", "show"],
            capture_output=True, text=True, timeout=2.0,
        )
        return "Discovering: yes" in proc.stdout
    except Exception:
        return False  # assume not discovering if check fails


def prepare_bluez_global() -> None:
    """Stop background scanning so raw LE connections can be initiated."""
    subprocess.run(["pkill", "-f", "decky-bluetooth-wake-control"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _run_quiet(["bluetoothctl", "scan", "off"], timeout=1.5)
    _run_quiet(
        ["busctl", "call", "org.bluez", f"/org/bluez/hci{_adapter_index()}",
         "org.bluez.Adapter1", "StopDiscovery"],
        timeout=1.5,
    )
    _force_bt_inquiry_off(force=True)
    _force_bt_le_scan_off(force=True)


def prepare_bluez(mac: str = "", *, remove: bool = False) -> None:
    prepare_bluez_global()
    if remove and mac:
        _bluez_remove_device(mac)


_REORDER_SCRIPTS = [
    "~/.local/bin/bazzite-dolphin-apply-gcpad1.sh",
    "~/.local/bin/bazzite-eden-reset-controllers.py",
]


def _reorder_enabled() -> bool:
    return os.environ.get("NGC_AUTO_REORDER", "1").lower() not in {"0", "false", "no"}


def _read_led_players() -> dict[str, int]:
    """MAC -> player (1-based) overrides from the LED sync tool."""
    try:
        raw = json.loads(_LED_PLAYERS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for mac, player in raw.items():
        try:
            out[str(mac).upper()] = int(player)
        except (TypeError, ValueError):
            continue
    return out


def _led_override_for(mac: str) -> Optional[int]:
    player = _read_led_players().get(mac.upper())
    if player is None:
        return None
    return min(max(player, 1), 8)


def run_emulator_reorder() -> None:
    if not _reorder_enabled():
        return
    for raw in _REORDER_SCRIPTS:
        path = Path(os.path.expanduser(raw))
        if not path.is_file():
            continue
        try:
            cmd = [str(path)]
            if path.suffix == ".py":
                cmd = ["python3", str(path)]
            subprocess.run(cmd, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("emulator reorder applied (%s)", path.name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("reorder hook %s failed: %s", path.name, exc)


class _ConnectHub:
    """Single BLE scanner that connects controllers the moment they advertise."""

    def __init__(self, config: Config, stop: threading.Event, bridge: Optional["Bridge"] = None):
        self.config = config
        self.stop = stop
        self.bridge = bridge
        self.host_mac = P.mac_to_int(config.adapter_mac) if config.adapter_mac else None
        self.workers_by_mac: dict[str, "_Worker"] = {}
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._scanner: Optional[BleakScanner] = None
        self._connect_lock: Optional[asyncio.Lock] = None
        self._last_seen: dict[str, tuple[float, str]] = {}
        self._logged: set[str] = set()
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._hub_error = ""
        self._scanning = False

    def register(self, worker: "_Worker") -> None:
        self.workers_by_mac[worker.entry.mac.upper()] = worker

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_async, name="ngc-hub", daemon=True)
        self._thread.start()

    def _accept(self, addr: str, adv) -> bool:
        worker = self.workers_by_mac.get(addr)
        if worker is None or worker.is_connected():
            return False
        reconnect = P.reconnect_mac_from_advertisement(adv)
        if reconnect is not None and self.host_mac is not None and reconnect not in (0, self.host_mac):
            return False
        return True

    def _run_async(self) -> None:
        # Keep ONE event loop for the hub's lifetime: bleak's
        # BlueZDBusScannerManager is a process-wide singleton whose D-Bus
        # connection binds to the loop it was first used on, so
        # asyncio.run() per attempt (fresh loop on every crash) left the
        # manager pinned to a dead loop. Loop reuse also makes repeated
        # scan bursts cheap.
        backoff = 1.0
        fails = 0
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while not self.stop.is_set():
                try:
                    self._hub_error = ""
                    loop.run_until_complete(self._scan_loop())
                    backoff, fails = 1.0, 0
                except Exception as exc:  # noqa: BLE001
                    self._hub_error = str(exc)
                    fails += 1
                    if fails == 1:
                        logger.exception("connect hub crashed; restarting in %ss", backoff)
                    else:
                        logger.warning(
                            "connect hub restart %d in %ss (%s)", fails, backoff, exc
                        )
                    if isinstance(exc, BleakDBusError) and "InProgress" in str(exc):
                        # Another client (Steam Input, decky wake plugin) holds
                        # the adapter's LE discovery; clear it at HCI level so
                        # the retry has a chance instead of crash-looping.
                        prepare_bluez_global()
                        _force_bt_le_scan_off(force=True)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    async def _scan_loop(self) -> None:
        hub = self
        hub._loop = asyncio.get_running_loop()
        hub._connect_lock = asyncio.Lock()
        if hub._executor is None or getattr(hub._executor, "_shutdown", False):
            hub._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="ngc-connect"
            )
        seen_ttl_s = 4.0

        def on_adv(device, adv) -> None:
            addr = device.address.upper()
            if hub.stop.is_set() or not hub._accept(addr, adv):
                return
            reconnect = P.reconnect_mac_from_advertisement(adv)
            mode = "pairing" if reconnect == 0 else "wake"
            hub._last_seen[addr] = (time.monotonic(), mode)
            if addr not in hub._logged:
                hub._logged.add(addr)
                logger.info("saw %s (%s)", addr, mode)

        hub._scanner = BleakScanner(detection_callback=on_adv)
        logger.info("scanning for configured controllers (press a button or hold Sync)")
        try:
            while not hub.stop.is_set():
                workers = list(hub.workers_by_mac.values())
                disconnected = [w for w in workers if not w.is_connected()]
                if not disconnected:
                    hub._scanning = False
                    await asyncio.sleep(1.5)
                    continue

                connected_count = len(workers) - len(disconnected)
                if connected_count:
                    # Scanning while a pad is linked can drop the live session; brief pause.
                    await asyncio.sleep(1.0)

                scan_on_s = 0.12 if connected_count else 0.25
                # If another client (Steam Input, decky wake plugin) holds the
                # adapter's LE discovery, start() fails immediately with
                # [org.bluez.Error.InProgress]. Clear the HCI-level scan and
                # retry here so a transient Steam scan doesn't kill the whole
                # hub loop; the _run_async backoff is the outer net.
                hub._scanning = True
                for scan_attempt in range(1, 4):
                    try:
                        await hub._scanner.start()
                        break
                    except BleakDBusError as exc:
                        if "InProgress" not in str(exc):
                            raise
                        hub._scanning = False
                        prepare_bluez_global()
                        _force_bt_le_scan_off(force=True)
                        logger.warning(
                            "LE scan held by another client; cleared, retry %d/3",
                            scan_attempt,
                        )
                        await asyncio.sleep(0.5 * scan_attempt)
                        hub._scanning = True
                else:
                    raise BleakDBusError(
                        "org.bluez.Error.InProgress",
                        "LE scan still held by another client after 3 clears",
                    )
                try:
                    await asyncio.sleep(scan_on_s)
                finally:
                    await hub._scanner.stop()
                    hub._scanning = False

                await asyncio.sleep(_SCAN_SETTLE_S)

                now = time.monotonic()
                pending = sorted(
                    [
                        mac for mac, worker in hub.workers_by_mac.items()
                        if not worker.is_connected()
                        and (seen := hub._last_seen.get(mac)) is not None
                        and now - seen[0] <= seen_ttl_s
                    ],
                    key=lambda mac: hub._last_seen[mac][0],
                    reverse=True,
                )
                if pending:
                    # Clear Steam's LE scan and drop BlueZ Device ghosts before dialing.
                    prepare_bluez_global()
                    for mac in pending:
                        worker = hub.workers_by_mac.get(mac)
                        if worker is not None and not worker.is_connected():
                            _bluez_remove_device(mac)
                    # Reconnect settle (joyfusion finding): the kernel/BlueZ
                    # hasn't finished tearing down the just-removed device's
                    # old L2CAP/ATT association when the next connect starts
                    # right after; joyfusion settled on 1.5s (500ms wasn't
                    # enough once retries got faster -- EBUSY). Applied ONLY
                    # to genuine reconnects (ever_connected); a first-ever
                    # connect must dial immediately, the Joy-Con 2 advertises
                    # for only ~10s when woken and a 1.5s delay makes it miss
                    # that window entirely.
                async with hub._connect_lock:
                    for mac in pending:
                        worker = hub.workers_by_mac.get(mac)
                        if worker is None or worker.is_connected():
                            hub._logged.discard(mac)
                            hub._last_seen.pop(mac, None)
                            continue
                        if worker.ever_connected:
                            await asyncio.sleep(1.5)
                        mode = hub._last_seen[mac][1]
                        try:
                            ok, detail = await hub._loop.run_in_executor(
                                hub._executor, hub._connect_sync, mac
                            )
                        except Exception as exc:  # noqa: BLE001
                            ok, detail = False, str(exc)
                        if ok:
                            logger.info("connected %s after %s advert", mac, mode)
                            hub._last_seen.pop(mac, None)
                            hub._logged.discard(mac)
                        else:
                            logger.info("connect to %s (%s) failed (%s)", mac, mode, detail)
                            _force_bt_inquiry_off(force=True)
                            # Not gated on worker.ever_connected -- a radio
                            # that's wedged before this process ever
                            # connected anything needs the same recovery a
                            # wedged-after-reconnecting one does. Confirmed
                            # on real hardware: right after a service
                            # restart, repeated first-ever-connect failures
                            # never triggered this (ever_connected was still
                            # False for every worker), so nothing broke the
                            # loop except a manual Bluetooth power cycle.
                            worker.reconnect_failures += 1
                            if (
                                worker.reconnect_failures
                                >= _RECONNECT_FAILURES_BEFORE_POWER_CYCLE
                                and not any(
                                    w.is_connected()
                                    for w in hub.workers_by_mac.values()
                                )
                            ):
                                logger.warning(
                                    "reconnects failing repeatedly with nothing left connected; "
                                    "power-cycling adapter to clear wedged radio state"
                                )
                                _power_cycle_adapter()
                                for w in hub.workers_by_mac.values():
                                    w.reconnect_failures = 0
                                hub._last_seen.clear()
                                hub._logged.clear()
                                # Adapter needs a moment to re-init;
                                # bail out of this pass -- the scan
                                # loop will re-see the pads on their
                                # next advertisement. Also avoids the
                                # KeyError the cleared _last_seen would
                                # cause on the next pending mac.
                                break

                for mac, (seen_at, _) in list(hub._last_seen.items()):
                    if now - seen_at > seen_ttl_s:
                        hub._last_seen.pop(mac, None)
                        hub._logged.discard(mac)

                await asyncio.sleep(0.05 if connected_count else 0.025)
        finally:
            hub._scanning = False
            if hub._scanner is not None:
                await hub._scanner.stop()

    def _connect_sync(self, mac: str) -> tuple[bool, str]:
        worker = self.workers_by_mac.get(mac)
        if worker is None or worker.is_connected():
            return False, "already connected"
        adapter = self.config.adapter_mac
        if not adapter:
            return False, "no adapter configured"
        attempt_s = _CONNECT_ATTEMPT_S
        # Every dst_type tried costs a real ~1.5s HCI-queue tax on top of the
        # poll wait itself (see _connect_dst_with_polling), regardless of
        # whether it succeeds, so trying the previously-successful dst_type
        # first (worker.last_dst_type) saves that cost in the common case.
        # Tried skipping the *other* type entirely once a hint exists --
        # reverted: on real hardware, a "wake"-mode advertisement (reconnect
        # to a specific bonded host) failed fast with a real SO_ERROR right
        # after this landed, immediately after a dst_type had just been
        # learned from a "pairing"-mode connect -- consistent with wake and
        # pairing mode not always using the same LE address type for the
        # same physical pad. Always try both; only the *order* is hinted.
        dst_types = (
            (worker.last_dst_type, att.LE_RANDOM if worker.last_dst_type == att.LE_PUBLIC else att.LE_PUBLIC)
            if worker.last_dst_type is not None
            else (att.LE_PUBLIC, att.LE_RANDOM)
        )
        with _CONNECT_LOCK:
            last_detail = "no attempts"
            for attempt in range(_CONNECT_ATTEMPTS):
                ctrl = SwitchController(mac, adapter)
                for dst in dst_types:
                    ok, detail = self._connect_dst_with_polling(ctrl, dst, attempt_s)
                    if ok:
                        ctrl.att.dst_type = dst
                        if worker.activate(ctrl):
                            worker._ready.set()
                            return True, "ok"
                        ctrl.close()
                        return False, "session setup failed"
                    last_detail = detail
                ctrl.close()
                time.sleep(0.02)
            if worker.last_dst_type is not None:
                worker.last_dst_type = None
            return False, last_detail

    @staticmethod
    def _connect_dst_with_polling(ctrl: SwitchController, dst: int, attempt_s: float) -> tuple[bool, str]:
        """Start one connect and poll it to completion or attempt_s, without
        touching btmgmt again once the connect is in flight.

        Tried re-suppressing BR/EDR Inquiry on every short sub-poll here
        (every _POLL_SUBINTERVAL_S) on the theory that Inquiry creeping back
        ~1s after being stopped needed to be re-cancelled repeatedly to keep
        a multi-second wait clear. Debug-instrumented on real hardware and
        disproven: each individual select() poll returned in exactly its
        requested time (no bug there), but a ~1.5s gap appeared *between*
        polls -- right where the per-poll _force_bt_inquiry_off() call sits.
        Sending a btmgmt command while our own LE Create Connection is
        already pending apparently has to wait on the same HCI command queue
        as that pending connect, so re-suppressing mid-wait was costing
        almost exactly the price it was trying to avoid. Suppressing once,
        before starting the connect, and then leaving the pending connect
        alone for the whole wait avoids that self-inflicted stall -- even
        though Inquiry may creep back in before this wait ends.
        """
        # Stop both BR/EDR and LE discovery right before connecting.
        # Do NOT call btmgmt during polling — it stalls the pending connect
        # on the HCI command queue. Call LE scan off first (it shares the
        # same HCI queue), then inquiry off last so it's the most recent.
        _force_bt_le_scan_off(force=True)
        _force_bt_inquiry_off(force=True)
        s, err = ctrl.att._start_connect(dst)
        if s is None:
            return False, err
        status, detail = ctrl.att._poll_connect(s, attempt_s)
        if status == "connected":
            ctrl.att._finish_connect(s)
            return True, "ok"
        s.close()
        if status == "pending":
            return False, "timeout (adapter may still be scanning)"
        return False, detail


class _PairGroup:
    """Shared virtual gamepad + DSU slot for a left+right Joy-Con 2 pair that
    share one player slot (see ControllerEntry.pair_role).

    Built once, on whichever half connects first, and lives for the whole
    bridge process -- Steam only notices a uinput device disappear on bridge
    restart anyway, so there is no benefit to tearing the shared pad down and
    recreating it as halves connect/disconnect. A disconnected half's
    contribution to the pad just idles at neutral (buttons up, stick
    centered) until it reconnects; the other half keeps working normally.
    """

    def __init__(
        self,
        player: int,
        dsu: Optional[DSUServer],
        on_topology_change: Optional[callable] = None,
    ):
        self.player = player
        self.dsu = dsu
        self.on_topology_change = on_topology_change
        self.slot = max(0, min(3, player - 1))
        self.gamepad: Optional[SwitchGamepad] = None
        self._lock = threading.Lock()
        self._connected = {"left": False, "right": False}
        self._mac = {"left": "", "right": ""}
        self._battery_mv: dict[str, Optional[int]] = {"left": None, "right": None}
        # Per-side latest (buttons, stick_xy, lt, rt); neutral until that
        # half has reported input or has disconnected.
        _neutral = (0, (0.0, 0.0), 0, 0)
        self._contrib = {"left": _neutral, "right": _neutral}
        self._last_report: Optional[P.InputReport] = None
        # Each physical half has its own HD-rumble motor; the shared pad has
        # one rumble_cb slot, so fan a single force-feedback signal out to
        # whichever half(s) are currently connected.
        self._rumble_targets: dict[str, Optional[Callable[[float, float], None]]] = {
            "left": None, "right": None,
        }

    def ensure_gamepad(self, name: str, mac: str, enable_rumble: bool) -> None:
        with self._lock:
            if self.gamepad is not None:
                return
            self.gamepad = SwitchGamepad(
                name=name, button_map=JOYCON2_PAIR_BUTTON_MAP, product=JOYCON2_PAIR_PID, mac=mac,
            )
            if enable_rumble:
                self.gamepad.rumble_cb = self._fanout_rumble
        logger.info("virtual gamepad ready (paired): %s", name)

    def set_rumble_target(self, side: str, cb: Optional[Callable[[float, float], None]]) -> None:
        with self._lock:
            self._rumble_targets[side] = cb

    def _fanout_rumble(self, strong: float, weak: float) -> None:
        with self._lock:
            targets = [cb for cb in self._rumble_targets.values() if cb is not None]
        for cb in targets:
            try:
                cb(strong, weak)
            except Exception:  # noqa: BLE001
                pass

    def set_connected(self, side: str, connected: bool, mac: str = "", battery_mv: Optional[int] = None) -> None:
        with self._lock:
            self._connected[side] = connected
            if connected:
                self._mac[side] = mac
                self._battery_mv[side] = battery_mv
            else:
                self._contrib[side] = (0, (0.0, 0.0), 0, 0)
            any_connected = self._connected["left"] or self._connected["right"]
            dsu_mac = self._mac["right"] or self._mac["left"]
            mvs = [v for v in self._battery_mv.values() if v]
            dsu_battery_mv = min(mvs) if mvs else 0
        self._push()
        if self.dsu is not None:
            self.dsu.set_slot(self.slot, any_connected, mac=dsu_mac, battery_mv=dsu_battery_mv)
        if self.on_topology_change is not None:
            self.on_topology_change()

    def update(self, side: str, report: P.InputReport, stick: tuple[float, float], lt: int, rt: int) -> None:
        with self._lock:
            self._contrib[side] = (report.buttons, stick, lt, rt)
            self._last_report = report
        self._push()

    def _push(self) -> None:
        with self._lock:
            lb, lstick, llt, lrt = self._contrib["left"]
            rb, rstick, rlt, rrt = self._contrib["right"]
            last_report = self._last_report
        combined_buttons = lb | rb
        lt, rt = max(llt, rlt), max(lrt, rrt)
        if self.gamepad is not None:
            self.gamepad.update(combined_buttons, lstick, rstick, lt, rt)
        if self.dsu is not None and last_report is not None:
            from dataclasses import replace

            # Motion (accel/gyro) can only come from one physical half at a
            # time -- DSU reflects whichever side's report arrived last.
            dsu_report = replace(last_report, buttons=combined_buttons)
            sticks = (
                _stick_to_dsu(lstick[0]), _stick_to_dsu(lstick[1]),
                _stick_to_dsu(rstick[0]), _stick_to_dsu(rstick[1]),
            )
            self.dsu.update(self.slot, dsu_report, sticks, (lt, rt))

    def close(self) -> None:
        if self.gamepad is not None:
            self.gamepad.release_all()
            self.gamepad.close()
            self.gamepad = None
        if self.dsu is not None:
            self.dsu.set_slot(self.slot, False)


class _Worker:
    """Owns input streaming, rumble, and virtual gamepad for one controller session."""

    def __init__(
        self,
        entry: ControllerEntry,
        config: Config,
        stop: threading.Event,
        hub: _ConnectHub,
        dsu: Optional[DSUServer] = None,
        on_topology_change: Optional[callable] = None,
        pair_group: Optional[_PairGroup] = None,
    ):
        self.entry = entry
        self.config = config
        self._stop = stop
        self.hub = hub
        self.dsu = dsu
        self.on_topology_change = on_topology_change
        self.pair_group = pair_group
        self.slot = max(0, min(3, entry.player - 1))
        self.gamepad: Optional[SwitchGamepad] = None
        self.motion: Optional[MotionEvdev] = None
        self._gamepad_product: Optional[int] = None
        self.controller: Optional[SwitchController] = None
        self._disconnected = threading.Event()
        self._ready = threading.Event()
        self._led_player: Optional[int] = None
        # Set once this mac has ever completed activate() successfully.
        # Distinguishes a real reconnect (needs the settle delay below --
        # see _scan_loop) from this process's first-ever connect to it,
        # which has no stale BlueZ/kernel L2CAP state to wait out.
        self.ever_connected = False
        # Consecutive failed reconnect passes since the last success or
        # power cycle; drives the adapter power-cycle recovery (see
        # _scan_loop).
        self.reconnect_failures = 0
        # dst_type (LE_PUBLIC/LE_RANDOM) that last actually worked for this
        # mac -- each fresh HCI-level connect/cancel on this hardware costs
        # a real ~1.5s tax regardless of how it's issued (see
        # _connect_dst_with_polling's doc comment), so trying both address
        # types on every reconnect roughly doubles how many of those we pay
        # for no reason once we already know which one this pad uses (see
        # _connect_sync).
        self.last_dst_type: Optional[int] = None

    def is_connected(self) -> bool:
        return self.controller is not None and self.controller.is_connected

    def effective_player(self) -> int:
        """Config player slot, optionally overridden by led-players.json."""
        override = _led_override_for(self.entry.mac)
        return override if override is not None else self.entry.player

    def _on_input(self, ctrl: SwitchController, report: P.InputReport) -> None:
        (lx, ly), (rx, ry), lt, rt = ctrl.calibrated_input(report)
        if self.pair_group is not None:
            # calibrated_input always reports a solo half's one physical
            # stick through the (lx, ly) slot (see device.calibrated_input);
            # route it to the pair's left_stick or right_stick output by
            # which physical half this worker actually is.
            stick = (lx, ly)
            self.pair_group.update(self.entry.pair_role, report, stick, lt, rt)
            if self.motion is not None:
                self.motion.update(report)
            return
        if self.gamepad is not None:
            self.gamepad.update(report.buttons, (lx, ly), (rx, ry), lt, rt)
        if self.motion is not None:
            self.motion.update(report)
        if self.dsu is not None:
            sticks = (_stick_to_dsu(lx), _stick_to_dsu(ly),
                      _stick_to_dsu(rx), _stick_to_dsu(ry))
            self.dsu.update(self.slot, report, sticks, (lt, rt))

    def _on_disconnect(self) -> None:
        logger.warning("controller %s disconnected", self.entry.mac)
        self._disconnected.set()

    def _on_rumble(self, strong: float, weak: float) -> None:
        ctrl = self.controller
        if ctrl is None or not ctrl.is_connected:
            return
        try:
            ctrl.set_rumble(strong, weak)
        except Exception as exc:  # noqa: BLE001
            logger.debug("rumble failed: %s", exc)

    def _ensure_gamepad(self, ctrl: SwitchController) -> None:
        pid = ctrl.product_id
        if self.gamepad is not None and self._gamepad_product == pid:
            return
        if self.gamepad is not None:
            self.gamepad.rumble_cb = None
            self.gamepad.close()
            self.gamepad = None
            self._gamepad_product = None
        if self.motion is not None:
            self.motion.close()
            self.motion = None
        name = f"{ctrl.name} (P{self.entry.player})"
        self.gamepad = SwitchGamepad(
            name=name,
            button_map=self.config_button_map(pid),
            product=pid,
            mac=self.entry.mac,
        )
        self.motion = MotionEvdev(name, self.entry.mac, product=pid)
        self._gamepad_product = pid
        logger.info("virtual gamepad ready: %s", name)

    def config_button_map(self, product_id: int):
        from .gamepad import button_map_for_product
        from evdev import ecodes as e

        if not self.config.button_map:
            return button_map_for_product(product_id)
        resolved = {}
        for switch_name, code in self.config.button_map.items():
            resolved[switch_name] = getattr(e, code) if isinstance(code, str) else code
        return resolved

    def activate(self, ctrl: SwitchController) -> bool:
        mac = self.entry.mac
        try:
            logger.info("connected to %s (MTU %d)", mac, ctrl.att.mtu)
            ctrl.input_callback = self._on_input
            ctrl.disconnect_callback = self._on_disconnect
            self._disconnected.clear()
            player = self.effective_player()
            ctrl.initialize(player=player)
            self._led_player = player
            if not self.entry.bonded:
                ctrl.bond()
                self.config.mark_bonded(mac, True)
                self.config.save()
                self.entry.bonded = True
                logger.info("bonded %s to %s", mac, self.config.adapter_mac)
            self.controller = ctrl
            if self.pair_group is not None:
                name = f"Joy-Con 2 (Pair) (P{self.entry.player})"
                self.pair_group.ensure_gamepad(name, mac, self.config.enable_rumble)
                self.pair_group.set_rumble_target(self.entry.pair_role, self._on_rumble)
                if self.motion is None:
                    self.motion = MotionEvdev(
                        f"{ctrl.name} (P{self.entry.player})", mac, product=ctrl.product_id
                    )
                self.pair_group.set_connected(
                    self.entry.pair_role, True, mac=mac, battery_mv=ctrl.battery_mv
                )
            else:
                self._ensure_gamepad(ctrl)
                if self.gamepad is not None and self.config.enable_rumble:
                    self.gamepad.rumble_cb = self._on_rumble
                if self.dsu is not None:
                    self.dsu.set_slot(self.slot, True, mac=mac, battery_mv=ctrl.battery_mv or 0)
                if self.on_topology_change is not None:
                    self.on_topology_change()
            if self.hub.bridge is not None:
                self.hub.bridge._publish_state()
            self.ever_connected = True
            self.reconnect_failures = 0
            self.last_dst_type = ctrl.att.dst_type
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("session setup failed for %s: %s", mac, exc)
            self._teardown_partial(ctrl)
            return False

    def _teardown_partial(self, ctrl: Optional[SwitchController] = None) -> None:
        if ctrl is not None:
            try:
                ctrl.close()
            except Exception:  # noqa: BLE001
                pass
        if self.pair_group is not None:
            self.pair_group.set_rumble_target(self.entry.pair_role, None)
            self.pair_group.set_connected(self.entry.pair_role, False)
        if self.gamepad is not None:
            self.gamepad.rumble_cb = None
            self.gamepad.close()
            self.gamepad = None
            self._gamepad_product = None
        if self.motion is not None:
            self.motion.close()
            self.motion = None

    def _teardown_session(self, *, full: bool = False) -> None:
        if self.pair_group is not None:
            self.pair_group.set_rumble_target(self.entry.pair_role, None)
            self.pair_group.set_connected(self.entry.pair_role, False)
            if full and self.motion is not None:
                self.motion.close()
                self.motion = None
        else:
            if self.gamepad is not None:
                self.gamepad.rumble_cb = None
                if full:
                    self.gamepad.close()
                    self.gamepad = None
                    self._gamepad_product = None
                else:
                    self.gamepad.release_all()
            if self.motion is not None:
                if full:
                    self.motion.close()
                    self.motion = None
            if self.dsu is not None:
                self.dsu.set_slot(self.slot, False)
        if self.controller:
            self.controller.close()
            self.controller = None
        if self.on_topology_change is not None:
            self.on_topology_change()
        if self.hub.bridge is not None:
            self.hub.bridge._publish_state()

    def run(self) -> None:
        self.hub.register(self)
        while not self._stop.is_set():
            self._ready.clear()
            while not self._stop.is_set() and not self._ready.wait(1.0):
                pass
            if self._stop.is_set() or not self.is_connected():
                continue
            while not self._stop.is_set() and not self._disconnected.is_set():
                self._disconnected.wait(0.5)
            self._teardown_session(full=False)

    def cleanup(self) -> None:
        self._teardown_session(full=True)


class Bridge:
    def __init__(self, config: Config):
        self.config = config
        self._stop = threading.Event()
        self.workers: list[_Worker] = []
        self.pair_groups: dict[int, _PairGroup] = {}
        self.dsu: Optional[DSUServer] = None
        self.hub = _ConnectHub(config, self._stop, bridge=self)
        self._reorder_timer: Optional[threading.Timer] = None
        self._reorder_lock = threading.Lock()
        self._state_lock = threading.Lock()

    def _battery_pct(self, mv: Optional[int]) -> Optional[int]:
        if not mv:
            return None
        return max(0, min(100, int((mv - 3300) * 100 / 900)))

    def _publish_state(self) -> None:
        entries = self.config.entries()
        connected = sum(1 for w in self.workers if w.is_connected())
        with self._state_lock:
            controllers: list[ControllerState] = []
            for entry in entries:
                worker = next(
                    (w for w in self.workers if w.entry.mac.upper() == entry.mac.upper()),
                    None,
                )
                ctrl = worker.controller if worker else None
                mv = ctrl.battery_mv if ctrl else None
                controllers.append(
                    ControllerState(
                        mac=entry.mac,
                        player=entry.player,
                        name=entry.name or (ctrl.name if ctrl else ""),
                        bonded=entry.bonded,
                        connected=worker.is_connected() if worker else False,
                        battery_pct=self._battery_pct(mv),
                    )
                )
            if self._stop.is_set():
                headline, detail, service = "Stopping", "", "stopping"
            elif self.hub._hub_error:
                headline, detail, service = "Needs attention", self.hub._hub_error[:120], "error"
            elif not entries:
                headline, detail, service = "Set up", "Add a controller once with Sync.", "running"
            elif connected:
                names = ", ".join(
                    f"P{c.player} {c.name or 'Controller'}"
                    for c in controllers if c.connected
                )
                headline = f"{connected} connected"
                detail = f"{names} — ready in Steam and emulators"
                service = "running"
            elif self.hub._scanning:
                headline = "Scanning"
                detail = "Press a button or hold Sync on a saved controller."
                service = "running"
            else:
                headline = "Ready"
                detail = "Press a button or hold Sync on a saved controller."
                service = "running"
            write_state(
                BridgeState(
                    hub_alive=not self._stop.is_set() and not self.hub._hub_error,
                    hub_scanning=self.hub._scanning,
                    hub_error=self.hub._hub_error,
                    service=service,
                    headline=headline,
                    detail=detail,
                    controllers=controllers,
                )
            )

    def _apply_led_overrides(self) -> None:
        """Push led-players.json slots onto connected bridge pads."""
        mapping = _read_led_players()
        if not mapping:
            return
        for worker in self.workers:
            ctrl = worker.controller
            if ctrl is None or not ctrl.is_connected:
                continue
            player = mapping.get(worker.entry.mac.upper())
            if player is None or worker._led_player == player:
                continue
            try:
                ctrl.set_player_leds(player)
                worker._led_player = player
                logger.info("player LEDs %s -> P%d (led-players.json)", worker.entry.mac, player)
            except Exception as exc:  # noqa: BLE001
                logger.debug("player LED update failed for %s: %s", worker.entry.mac, exc)

    def _state_loop(self) -> None:
        while not self._stop.wait(_STATUS_INTERVAL_S):
            try:
                self._apply_led_overrides()
                self._publish_state()
            except Exception as exc:  # noqa: BLE001
                logger.debug("state publish failed: %s", exc)

    def _schedule_reorder(self) -> None:
        if self._stop.is_set():
            return
        with self._reorder_lock:
            if self._reorder_timer is not None:
                self._reorder_timer.cancel()
            # 15s (was 2s): on Bazzite, firing this right after connect races
            # bazzite-eden-reset-controllers.py, which drops the controller it
            # just connected. Give the link time to settle first.
            self._reorder_timer = threading.Timer(15.0, run_emulator_reorder)
            self._reorder_timer.daemon = True
            self._reorder_timer.start()

    def run(self) -> None:
        entries = self.config.entries()
        if not entries:
            raise RuntimeError("no controllers configured (run pairing first)")

        prepare_bluez_global()

        self.dsu = DSUServer()
        if not self.dsu.start():
            self.dsu = None

        self.hub.start()
        self._publish_state()
        threading.Thread(target=self._state_loop, name="ngc-state", daemon=True).start()
        logger.info("starting %d controller worker(s)", len(entries))
        for entry in entries:
            pair_group = None
            if entry.pair_role:
                pair_group = self.pair_groups.get(entry.player)
                if pair_group is None:
                    pair_group = _PairGroup(entry.player, self.dsu, on_topology_change=self._schedule_reorder)
                    self.pair_groups[entry.player] = pair_group
            worker = _Worker(
                entry,
                self.config,
                self._stop,
                self.hub,
                dsu=self.dsu,
                on_topology_change=self._schedule_reorder,
                pair_group=pair_group,
            )
            self.workers.append(worker)
            threading.Thread(target=worker.run, name=f"ctrl-{entry.player}", daemon=True).start()

        while not self._stop.is_set():
            self._stop.wait(0.5)

        with self._reorder_lock:
            if self._reorder_timer is not None:
                self._reorder_timer.cancel()
        for worker in self.workers:
            worker.cleanup()
        for pair_group in self.pair_groups.values():
            pair_group.close()
        if self.dsu is not None:
            self.dsu.stop()
        if self.hub._executor is not None:
            self.hub._executor.shutdown(wait=False, cancel_futures=True)
        clear_state()

    def stop(self) -> None:
        self._stop.set()

    def pulse_gamecube_hotkey(self, *switch_names: str, hold_s: float = 0.12) -> None:
        """Briefly press mapped buttons on connected GameCube pads (e.g. C+R for Dolphin save)."""
        masks = 0
        for name in switch_names:
            masks |= P.SWITCH_BUTTONS.get(name, 0)
        if not masks:
            return
        for worker in self.workers:
            gp = worker.gamepad
            ctrl = worker.controller
            if gp is None or ctrl is None or not worker.is_connected():
                continue
            if ctrl.product_id != P.NSO_GAMECUBE_PID:
                continue
            gp.update(masks, (0.0, 0.0), (0.0, 0.0), 0, 0)
            time.sleep(hold_s)
            gp.update(0, (0.0, 0.0), (0.0, 0.0), 0, 0)
