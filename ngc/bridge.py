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
from .gamepad import SwitchGamepad, JOYCON2_COMBINED_BUTTON_MAP
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
# Per-attempt L2CAP connect wait. Short windows fail when Steam keeps LE scan
# busy; after btmgmt stop-find -l a few hundred ms is enough.
_CONNECT_ATTEMPT_S = 0.45
_CONNECT_ATTEMPTS = 16


def _adapter_index() -> str:
    """Prefer hci0; allow override via NGC_HCI (e.g. '1')."""
    return os.environ.get("NGC_HCI", "0").strip() or "0"


_BTMGMT_LOCK = threading.Lock()
_LAST_LE_SCAN_OFF = 0.0
_LE_SCAN_OFF_MIN_INTERVAL_S = 0.35


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


def _force_le_scan_off(*, force: bool = False) -> None:
    """HCI-level LE discovery stop.

    BlueZ ``StopDiscovery`` only ends *our* session. Steam/steamos-manager keeps
    its own session forever (``Discovering`` stays true), which blocks raw L2CAP
    create-connection. ``btmgmt stop-find -l`` stops the controller's LE scan
    regardless of who started it. Requires passwordless ``sudo`` for btmgmt
    (Bazzite default for this user).

    Never raises — a hung btmgmt must not crash the bridge.
    """
    global _LAST_LE_SCAN_OFF
    now = time.monotonic()
    with _BTMGMT_LOCK:
        if not force and (now - _LAST_LE_SCAN_OFF) < _LE_SCAN_OFF_MIN_INTERVAL_S:
            return
        _LAST_LE_SCAN_OFF = now
        idx = _adapter_index()
        # Start detached-ish: kill hung btmgmt so we never block the hub.
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
    _force_le_scan_off(force=True)


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
                    await asyncio.sleep(0.08)
                async with hub._connect_lock:
                    for mac in pending:
                        worker = hub.workers_by_mac.get(mac)
                        if worker is None or worker.is_connected():
                            hub._logged.discard(mac)
                            hub._last_seen.pop(mac, None)
                            continue
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
                            _force_le_scan_off(force=True)

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
        with _CONNECT_LOCK:
            _force_le_scan_off()
            last_detail = "no attempts"
            for attempt in range(_CONNECT_ATTEMPTS):
                if attempt and attempt % 4 == 0:
                    _force_le_scan_off()
                ctrl = SwitchController(mac, adapter)
                for dst in (att.LE_PUBLIC, att.LE_RANDOM):
                    ok, detail = ctrl.att._connect_once(dst, _CONNECT_ATTEMPT_S)
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
            return False, last_detail


class _PairCoordinator:
    """Merges a Left+Right Joy-Con 2 pair (same player slot) into one full
    dual-stick virtual gamepad, instead of each half getting its own.

    One instance per player slot (Bridge keys them by slot, see
    Bridge.get_or_create_joycon_pair), so multiple simultaneous pairs (P1
    dual + P2 dual, for local multiplayer) are supported. Two independent
    _Worker instances (one per physical Joy-Con) each keep managing their
    own BLE connection/reconnection through the normal hub path; only
    gamepad/motion/DSU ownership is redirected here. Which worker feeds
    which pair (or neither, i.e. solo) is decided live by
    Bridge._retopologize(), so this can change without a BLE reconnect.
    """

    def __init__(
        self,
        slot: int,
        player: int,
        config: Config,
        dsu: Optional[DSUServer],
        on_topology_change: Optional[callable],
    ):
        self.player = player
        self.config = config
        self.dsu = dsu
        self.on_topology_change = on_topology_change
        self.slot = slot
        self.gamepad: Optional[SwitchGamepad] = None
        self.motion: Optional[MotionEvdev] = None
        self._sides: dict[str, "_Worker"] = {}
        self._buttons: dict[str, int] = {"left": 0, "right": 0}
        self._sticks: dict[str, tuple[float, float]] = {"left": (0.0, 0.0), "right": (0.0, 0.0)}
        self._lock = threading.Lock()

    @staticmethod
    def side_for_pid(pid: int) -> Optional[str]:
        if pid == P.JOYCON2_LEFT_PID:
            return "left"
        if pid == P.JOYCON2_RIGHT_PID:
            return "right"
        return None

    def activate_side(self, side: str, worker: "_Worker") -> None:
        with self._lock:
            self._sides[side] = worker
            mac = worker.entry.mac
            battery = worker.controller.battery_mv if worker.controller else 0
        if self.dsu is not None:
            self.dsu.set_slot(self.slot, True, mac=mac, battery_mv=battery or 0)
        if self.on_topology_change is not None:
            self.on_topology_change()

    def _ensure_gamepad(self) -> SwitchGamepad:
        if self.gamepad is None:
            name = f"Joy-Con 2 (Dual) (P{self.player})"
            self.gamepad = SwitchGamepad(
                name=name,
                button_map=JOYCON2_COMBINED_BUTTON_MAP,
                product=P.PRO_CONTROLLER2_PID,
                mac="",
            )
            self.motion = MotionEvdev(name, "", product=P.PRO_CONTROLLER2_PID)
            if self.config.enable_rumble:
                self.gamepad.rumble_cb = self._on_rumble
            logger.info("combined virtual gamepad ready: %s", name)
        return self.gamepad

    def _on_rumble(self, strong: float, weak: float) -> None:
        with self._lock:
            workers = list(self._sides.values())
        for worker in workers:
            ctrl = worker.controller
            if ctrl is None or not ctrl.is_connected:
                continue
            try:
                ctrl.set_rumble(strong, weak)
            except Exception as exc:  # noqa: BLE001
                logger.debug("pair rumble failed: %s", exc)

    def on_input(self, side: str, ctrl: SwitchController, report: P.InputReport) -> None:
        with self._lock:
            (lx, ly), (rx, ry), _lt, _rt = ctrl.calibrated_input(report)
            if side == "left":
                self._sticks["left"] = (lx, ly)
            else:
                self._sticks["right"] = (rx, ry)
            self._buttons[side] = report.buttons
            combined = self._buttons["left"] | self._buttons["right"]
            lt = 255 if combined & P.SWITCH_BUTTONS["ZL"] else 0
            rt = 255 if combined & P.SWITCH_BUTTONS["ZR"] else 0
            left_stick = self._sticks["left"]
            right_stick = self._sticks["right"]
            gp = self._ensure_gamepad()
            gp.update(combined, left_stick, right_stick, lt, rt)
            motion = self.motion
        if motion is not None:
            motion.update(report)
        if self.dsu is not None:
            sticks = (
                _stick_to_dsu(left_stick[0]), _stick_to_dsu(left_stick[1]),
                _stick_to_dsu(right_stick[0]), _stick_to_dsu(right_stick[1]),
            )
            self.dsu.update(self.slot, report, sticks, (lt, rt))

    def teardown_side(self, side: str, *, full: bool) -> None:
        with self._lock:
            self._sides.pop(side, None)
            empty = not self._sides
        if not empty:
            return
        if self.gamepad is not None:
            self.gamepad.rumble_cb = None
            if full:
                self.gamepad.close()
                self.gamepad = None
            else:
                self.gamepad.release_all()
        if full and self.motion is not None:
            self.motion.close()
            self.motion = None
        if self.dsu is not None:
            self.dsu.set_slot(self.slot, False)
        if self.on_topology_change is not None:
            self.on_topology_change()


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
        pair: Optional[_PairCoordinator] = None,
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
        self.pair = pair
        self._pair_side: Optional[str] = None

    def is_connected(self) -> bool:
        return self.controller is not None and self.controller.is_connected

    def effective_player(self) -> int:
        """Config player slot, optionally overridden by led-players.json."""
        override = _led_override_for(self.entry.mac)
        return override if override is not None else self.entry.player

    def _on_input(self, ctrl: SwitchController, report: P.InputReport) -> None:
        if self._pair_side is not None and self.pair is not None:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "%s (paired %s) raw buttons=%s",
                    self.entry.mac, self._pair_side, report.pressed(),
                )
            self.pair.on_input(self._pair_side, ctrl, report)
            return
        (lx, ly), (rx, ry), lt, rt = ctrl.calibrated_input(report)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "%s raw buttons=%s left_raw=%s right_raw=%s -> L=(%.2f,%.2f) R=(%.2f,%.2f) lt=%d rt=%d",
                self.entry.mac, report.pressed(), report.left_stick_raw, report.right_stick_raw,
                lx, ly, rx, ry, lt, rt,
            )
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
        """Create/refresh this worker's own solo virtual pad.

        Whether it actually stays solo or gets folded into a combined pair
        is decided separately, live, by Bridge._retopologize() (called right
        after this from activate(), and again on every state-loop tick) --
        not here, so that a later player-slot change can flip the mode
        without a BLE reconnect.
        """
        pid = ctrl.product_id
        if self.gamepad is not None and self._gamepad_product == pid:
            return
        self._close_gamepad()
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

    def _close_gamepad(self) -> None:
        if self.gamepad is not None:
            self.gamepad.rumble_cb = None
            self.gamepad.close()
            self.gamepad = None
            self._gamepad_product = None
        if self.motion is not None:
            self.motion.close()
            self.motion = None

    def join_pair(self, pair: "_PairCoordinator", side: str) -> None:
        """Switch this worker from solo (or a different pair) into `pair`,
        live -- no BLE reconnect. Called only by Bridge._retopologize()."""
        if self._pair_side == side and self.pair is pair:
            return
        if self._pair_side is not None:
            self.leave_pair(_retopo_will_follow=True)
        else:
            self._close_gamepad()
        ctrl = self.controller
        if ctrl is not None:
            ctrl.combined_mode = True
        self.pair = pair
        self._pair_side = side
        pair.activate_side(side, self)
        logger.info(
            "dual mode: %s joined as %s half of the combined pad (P%d)",
            self.entry.mac, side, pair.player,
        )

    def leave_pair(self, *, _retopo_will_follow: bool = False) -> None:
        """Switch this worker back to a solo pad, live -- no BLE reconnect.
        Called only by Bridge._retopologize() (directly, or via join_pair()
        when moving straight from one pair to another)."""
        if self._pair_side is None or self.pair is None:
            return
        pair, side = self.pair, self._pair_side
        self.pair = None
        self._pair_side = None
        pair.teardown_side(side, full=True)
        ctrl = self.controller
        if ctrl is not None:
            ctrl.combined_mode = False
        if not _retopo_will_follow and ctrl is not None and self.is_connected():
            self._ensure_gamepad(ctrl)
            if self.gamepad is not None and self.config.enable_rumble:
                self.gamepad.rumble_cb = self._on_rumble
            if self.dsu is not None:
                self.dsu.set_slot(self.slot, True, mac=self.entry.mac,
                                   battery_mv=ctrl.battery_mv or 0)
            logger.info("dual mode: %s split back to a solo pad", self.entry.mac)

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
            if self.hub.bridge is not None:
                # May immediately fold this worker into a combined pad if a
                # same-slot opposite-side Joy-Con is already connected --
                # see Bridge._retopologize(). Sets self._pair_side as a
                # side effect, so check it fresh below.
                self.hub.bridge._retopologize()
            if self._pair_side is None:
                # Paired mode already wired up rumble/DSU/topology in
                # _PairCoordinator.activate_side() (via _retopologize above).
                if self.gamepad is not None and self.config.enable_rumble:
                    self.gamepad.rumble_cb = self._on_rumble
                if self.dsu is not None:
                    self.dsu.set_slot(self.slot, True, mac=mac, battery_mv=ctrl.battery_mv or 0)
                if self.on_topology_change is not None:
                    self.on_topology_change()
            if self.hub.bridge is not None:
                self.hub.bridge._publish_state()
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
        if self._pair_side is not None and self.pair is not None:
            self.pair.teardown_side(self._pair_side, full=True)
            self._pair_side = None
            return
        if self.gamepad is not None:
            self.gamepad.rumble_cb = None
            self.gamepad.close()
            self.gamepad = None
            self._gamepad_product = None
        if self.motion is not None:
            self.motion.close()
            self.motion = None

    def _teardown_session(self, *, full: bool = False) -> None:
        if self._pair_side is not None and self.pair is not None:
            self.pair.teardown_side(self._pair_side, full=full)
            if full:
                self._pair_side = None
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
        self.dsu: Optional[DSUServer] = None
        self.hub = _ConnectHub(config, self._stop, bridge=self)
        self._reorder_timer: Optional[threading.Timer] = None
        self._reorder_lock = threading.Lock()
        self._state_lock = threading.Lock()
        # Keyed by 0-based player slot so multiple simultaneous pairs (P1
        # dual + P2 dual, local multiplayer) are possible. Which worker
        # feeds which pair -- or none, i.e. solo -- is decided live by
        # _retopologize(), not at connect time, so a player-slot change
        # (e.g. from led-players.json) can flip dual/single mode without a
        # BLE reconnect or service restart.
        self._joycon_pairs: dict[int, _PairCoordinator] = {}
        self._joycon_pair_lock = threading.Lock()
        # Non-reentrant lock: _retopologize() can be re-entered from within
        # itself (join_pair/leave_pair fire on_topology_change -> here
        # again) via two different threads (a hub connect thread and the
        # ngc-state timer thread), so a plain bool flag isn't safe here.
        self._retopo_lock = threading.Lock()

    def get_or_create_joycon_pair(self, slot: int, player: int) -> _PairCoordinator:
        """The Left+Right Joy-Con 2 pair for one player slot, created lazily
        on whichever half first needs it. Reused (not recreated) across
        combine/split/recombine cycles for the same slot."""
        with self._joycon_pair_lock:
            pair = self._joycon_pairs.get(slot)
            if pair is None:
                pair = _PairCoordinator(
                    slot, player, self.config, self.dsu, self._on_topology_change
                )
                self._joycon_pairs[slot] = pair
            else:
                pair.player = player
            return pair

    def _on_topology_change(self) -> None:
        self._retopologize()
        self._schedule_reorder()

    def _retopologize(self) -> None:
        """Reconcile Joy-Con L/R combine/split with each connected
        controller's *current* effective player slot.

        Runs live, with no BLE reconnect and no service restart -- only the
        affected worker's virtual pad is closed and reopened. Two Joy-Cons
        (one left, one right) sharing a slot combine into one dual-stick
        pad; anything else -- including a lone half whose partner just
        disconnected or moved to a different slot -- ends up solo. Called
        after every connect/disconnect (via on_topology_change) and once per
        state-loop tick, so a led-players.json slot change (see
        _apply_led_overrides) takes effect within one tick, not just at the
        next reconnect.
        """
        if not self.config.dual_mode:
            return
        if not self._retopo_lock.acquire(blocking=False):
            return
        try:
            left_by_slot: dict[int, _Worker] = {}
            right_by_slot: dict[int, _Worker] = {}
            for worker in self.workers:
                ctrl = worker.controller
                if ctrl is None or not worker.is_connected():
                    continue
                side = _PairCoordinator.side_for_pid(ctrl.product_id)
                if side is None:
                    continue
                slot = max(0, min(3, worker.effective_player() - 1))
                (left_by_slot if side == "left" else right_by_slot)[slot] = worker

            paired_slots = set(left_by_slot) & set(right_by_slot)

            for slot in paired_slots:
                left, right = left_by_slot[slot], right_by_slot[slot]
                player = min(left.effective_player(), right.effective_player())
                pair = self.get_or_create_joycon_pair(slot, player)
                if left.pair is not pair or left._pair_side != "left":
                    left.join_pair(pair, "left")
                if right.pair is not pair or right._pair_side != "right":
                    right.join_pair(pair, "right")

            for worker in self.workers:
                ctrl = worker.controller
                if ctrl is None or not worker.is_connected() or worker._pair_side is None:
                    continue
                side = _PairCoordinator.side_for_pid(ctrl.product_id)
                slot = max(0, min(3, worker.effective_player() - 1))
                if side is None or slot not in paired_slots:
                    worker.leave_pair()
        finally:
            self._retopo_lock.release()

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
                # Picks up player-slot changes even without a fresh
                # connect/disconnect event (e.g. led-players.json rewritten
                # by the LED-sync tool while both halves stay connected).
                self._retopologize()
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
        if self.config.dual_mode:
            logger.info(
                "dual mode enabled: Joy-Con 2 Left+Right pairs sharing a player "
                "slot merge into one pad; this is re-evaluated live, so changing "
                "either half's player slot re-splits or re-merges it without a "
                "reconnect"
            )

        for entry in entries:
            worker = _Worker(
                entry,
                self.config,
                self._stop,
                self.hub,
                dsu=self.dsu,
                on_topology_change=self._on_topology_change,
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
