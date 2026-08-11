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
from typing import Optional

from bleak import BleakScanner

from . import att
from . import protocol as P
from .config import CONFIG_DIR, Config, ControllerEntry
from .device import SwitchController
from .dsu import DSUServer
from .gamepad import SwitchGamepad
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
_SCAN_SETTLE_S = 0.10
# Time budget to poll ONE pending connect (one dst_type within one outer
# attempt) before giving up on it -- see _connect_dst_with_polling, whose
# doc comment covers the debug-instrumented real-hardware finding that
# re-suppressing BR/EDR Inquiry *during* this wait costs more than it
# saves (a btmgmt call issued while our own LE Create Connection is
# pending stalls on the same HCI command queue as that pending connect).
# Suppressed once before the connect starts and then left alone.
_CONNECT_ATTEMPT_S = 3.0
# Each dst_type tried costs ~1.5s (HCI-queue tax) + up to _CONNECT_ATTEMPT_S
# regardless of outcome (see _connect_dst_with_polling), so total budget
# here is _CONNECT_ATTEMPTS x len(dst_types tried) x ~4.5s. Once a pad's
# dst_type is known (see _connect_sync's worker.last_dst_type), that's just
# one type, so 2 attempts is ~9s -- inside the Joy-Con 2's ~10s wake-mode
# advertising window. The scan loop retries on the pad's next advertisement
# regardless, so a slower first-ever-pairing pass (unknown dst_type, both
# tried) isn't fatal, just not expected to finish within one window.
_CONNECT_ATTEMPTS = 2


def _adapter_index() -> str:
    """Prefer hci0; allow override via NGC_HCI (e.g. '1')."""
    return os.environ.get("NGC_HCI", "0").strip() or "0"


_BTMGMT_LOCK = threading.Lock()
_LAST_BT_INQUIRY_OFF = 0.0
_BT_INQUIRY_OFF_MIN_INTERVAL_S = 0.35

# Consecutive failed reconnect passes for a pad that previously connected
# before we power-cycle the adapter (see _power_cycle_adapter). Each pass is
# _CONNECT_ATTEMPTS x _CONNECT_ATTEMPT_S ~ 7s, so this fires after ~15s of
# continuous failure, matching the "only a power cycle clears it" symptom
# observed on real hardware.
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


# Tried toggling BR/EDR off for the duration of a connect attempt (to make
# Inquiry structurally impossible instead of racing to re-cancel it -- see
# _connect_dst_with_polling's doc comment for the Inquiry-restarts-in-~3ms
# btmon finding this was responding to). Doesn't work: ``btmgmt bredr off``
# is rejected (status 0x0b) while the adapter is powered on -- BlueZ only
# allows changing BR/EDR mode with the controller powered off, i.e. this
# has the same real cost as _power_cycle_adapter (drops every other
# Bluetooth device on the adapter too), which is exactly what this was
# meant to avoid. Abandoned.


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
        while not self.stop.is_set():
            try:
                self._hub_error = ""
                asyncio.run(self._scan_loop())
            except Exception as exc:  # noqa: BLE001
                self._hub_error = str(exc)
                logger.exception("connect hub crashed; restarting in 1s")
                time.sleep(1)

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
                hub._scanning = True
                await hub._scanner.start()
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
                            if worker.ever_connected:
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
        # Every dst_type tried costs a real ~1.5s HCI-queue tax on top of the
        # poll wait itself (see _connect_dst_with_polling), regardless of
        # whether it succeeds -- trying both LE_PUBLIC and LE_RANDOM every
        # single reconnect roughly doubles that cost for no reason once we
        # already know which one this pad actually uses. Once we have that
        # confirmed (worker.last_dst_type), only try it; if every attempt
        # still fails, clear the hint so the next _connect_sync call (next
        # scan pass) falls back to trying both again instead of getting
        # stuck on a hint that stopped being true.
        dst_types = (worker.last_dst_type,) if worker.last_dst_type is not None else (att.LE_PUBLIC, att.LE_RANDOM)
        with _CONNECT_LOCK:
            last_detail = "no attempts"
            for attempt in range(_CONNECT_ATTEMPTS):
                ctrl = SwitchController(mac, adapter)
                for dst in dst_types:
                    ok, detail = self._connect_dst_with_polling(ctrl, dst)
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
    def _connect_dst_with_polling(ctrl: SwitchController, dst: int) -> tuple[bool, str]:
        """Start one connect and poll it to completion or _CONNECT_ATTEMPT_S,
        without touching btmgmt again once the connect is in flight.

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
        _force_bt_inquiry_off()
        s, err = ctrl.att._start_connect(dst)
        if s is None:
            return False, err
        status, detail = ctrl.att._poll_connect(s, _CONNECT_ATTEMPT_S)
        if status == "connected":
            ctrl.att._finish_connect(s)
            return True, "ok"
        s.close()
        if status == "pending":
            return False, "timeout (adapter may still be scanning)"
        return False, detail


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
    ):
        self.entry = entry
        self.config = config
        self._stop = stop
        self.hub = hub
        self.dsu = dsu
        self.on_topology_change = on_topology_change
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
        if self.gamepad is not None:
            self.gamepad.rumble_cb = None
            self.gamepad.close()
            self.gamepad = None
            self._gamepad_product = None
        if self.motion is not None:
            self.motion.close()
            self.motion = None

    def _teardown_session(self, *, full: bool = False) -> None:
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
            self._reorder_timer = threading.Timer(2.0, run_emulator_reorder)
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
            worker = _Worker(
                entry,
                self.config,
                self._stop,
                self.hub,
                dsu=self.dsu,
                on_topology_change=self._schedule_reorder,
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
