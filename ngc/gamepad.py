"""Linux virtual gamepad (uinput) for Switch 2 controllers.

Presents a standard dual-stick gamepad with analog triggers (GameCube) or
digital ZL/ZR (Pro / Joy-Con) so SDL, Steam Input, and emulators recognise
pads without custom kernel support.
"""

from __future__ import annotations

import logging
import struct
import threading
from typing import Callable, Optional

from evdev import UInput, AbsInfo, ecodes as e

from . import protocol as P
from .motion_evdev import phys_for_mac

logger = logging.getLogger(__name__)

# struct input_event on 64-bit Linux: timeval(2*long) + type + code + value
_EVENT_FMT = "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)

# Axis ranges.
STICK_MIN, STICK_MAX = -32768, 32767
TRIGGER_MIN, TRIGGER_MAX = 0, 255

# --------------------------------------------------------------------------- #
# Evdev maps                                                                   #
#                                                                             #
# BLE input uses the unified Switch 2 bitmask in protocol.SWITCH_BUTTONS      #
# (Nadeflore/switch2-controllers, bitaxislabs). On the NSO GameCube pad the   #
# ZR bit (0x80) is physical Z; R bit (0x40) is R trigger full-click. For Steam /
# Switch-style layouts, ZL+ L -> BTN_TL (left bumper) and Z+ R click -> BTN_TR
# (right bumper). C -> BTN_SELECT (minus). Analog L/R stay on trigger axes.      #
#                                                                             #
# Face buttons use semantic evdev positions (A=SOUTH, B=EAST, X=WEST, Y=NORTH).
# BTN_C (306) sits between B and the face cluster, so SDL's auto gamecontrollerdb
# assigns x:b3/y:b4 to the wrong indices — install-emulator-integration.sh writes
# a corrected mapping for Steam. Dolphin uses WEST/NORTH tokens directly.
#
# NSO GameCube has Start (PLUS bit) and Home (HOME bit), no Select/MINUS.
# Capture/C need misc slots in gamecontrollerdb.
#
# A lone Joy-Con 2 only has half the buttons of a full pad and was previously
# reusing PRO_BUTTON_MAP wholesale (phantom keys for buttons that don't exist,
# and none of its own SL/SR/GL/GR rail buttons). JOYCON2_LEFT/RIGHT_BUTTON_MAP
# below mirror the upstream Linux kernel's hid-nintendo.c solo-Joy-Con tables
# (left_joycon_button_mappings / right_joycon_button_mappings): the unused
# opposite-side shoulder slot is reused for SL/SR so a single Joy-Con still
# exposes two "shoulder" buttons. GL/GR (Switch 2's new rail buttons, absent
# from the Switch 1 driver) go to BTN_C, same slot the GameCube map uses for
# its extra C button.
# --------------------------------------------------------------------------- #

PRO_BUTTON_MAP = {
    "A": e.BTN_SOUTH,
    "B": e.BTN_EAST,
    "X": e.BTN_WEST,
    "Y": e.BTN_NORTH,
    "L": e.BTN_TL,
    "R": e.BTN_TR,
    "ZL": e.BTN_TL2,
    "ZR": e.BTN_TR2,
    "PLUS": e.BTN_START,
    "MINUS": e.BTN_SELECT,
    "HOME": e.BTN_MODE,
    "CAPTURE": e.BTN_Z,
    "C": e.BTN_C,
    "L_STK": e.BTN_THUMBL,
    "R_STK": e.BTN_THUMBR,
}

# ZL/Z and L/R click share shoulder slots so Steam and emulators see standard bumpers.
GAMECUBE_BUTTON_MAP = {
    "A": e.BTN_SOUTH,
    "B": e.BTN_EAST,
    # NSO GC face layout: X is left (north on diamond), Y is top (west on diamond).
    "X": e.BTN_NORTH,
    "Y": e.BTN_WEST,
    "L": e.BTN_TL,
    "R": e.BTN_TR,
    "ZL": e.BTN_TL,       # left bumper (with L click)
    "ZR": e.BTN_TR,       # right bumper / physical Z (with R click)
    "PLUS": e.BTN_START,   # GC Start; BLE PLUS bit (not MINUS)
    "HOME": e.BTN_MODE,    # GC Nintendo/Home
    "CAPTURE": e.BTN_Z,    # screenshot; b4 -> misc2 in gamecontrollerdb
    "C": e.BTN_SELECT,     # minus / back (GC has no Select; C fills that slot)
    "L_STK": e.BTN_THUMBL,
    "R_STK": e.BTN_THUMBR,
}

# Solo Right Joy-Con: A/B/X/Y + R/ZR are its own; SL_R/SR_R (the rail buttons
# facing the missing left half) fill the L/ZL shoulder slots. Its one stick is
# presented as the primary stick by device.calibrated_input(), so the stick
# click lands on THUMBL (left-stick click), not THUMBR.
#
# Hardware testing (see -v raw traces) showed this Joy-Con 2 unit's face
# buttons fire the SWITCH_BUTTONS bits one position off from the shared
# GC/Pro table: pressing A sets the "X" bit, X sets the "B" bit, and B sets
# the "A" bit (Y is unaffected). GC/Pro don't show this, so it's kept as a
# Joy-Con-2-only key rotation here rather than touched in protocol.py.
#
# A raw -v trace confirmed R/SR_R fire their own correct bits (no hardware
# quirk there).
#
# SDL's own HandleMiniControllerStateR (src/joystick/hidapi/SDL_hidapi_switch.c,
# Valve-contributed) treats SR_R/SL_R as RIGHT_SHOULDER/LEFT_SHOULDER (matches
# below) and R/ZR as their own RIGHT_PADDLE1/2 type, distinct from
# shoulder/trigger. BTN_TRIGGER_HAPPY1/2 looked like the natural evdev
# equivalent for that "extra paddle" concept, but using it broke Steam's
# recognition of the whole device (even PLUS/HOME/R_STK, untouched by that
# change, started reading scrambled) -- an unfamiliar code apparently
# confuses whatever heuristic Steam uses here more than a wrong-but-standard
# gamepad code does. R/ZR stay on BTN_TL2/TR2 (no dedicated Steam indicator,
# but at least a stable, distinct signal) until a code that doesn't break
# device recognition is found.
#
# The evdev targets use the kernel driver's Nintendo-position convention
# (A=east, B=south, X=north, Y=west), NOT this project's usual Xbox-style
# positions: hardware testing confirmed this is the combination that reads
# correctly. (Steam's "Nintendo button layout" toggle also affects this on
# Steam specifically -- see the README note -- but the evdev-level mapping
# below is what tested correct.)
JOYCON2_RIGHT_BUTTON_MAP = {
    "X": e.BTN_EAST,    # physical A fires the "X" bit
    "B": e.BTN_NORTH,   # physical X fires the "B" bit
    "A": e.BTN_SOUTH,   # physical B fires the "A" bit
    "Y": e.BTN_WEST,
    "SR_R": e.BTN_TR,   # RIGHT_SHOULDER
    "SL_R": e.BTN_TL,   # LEFT_SHOULDER
    "R": e.BTN_TL2,
    "ZR": e.BTN_TR2,
    "PLUS": e.BTN_START,
    "HOME": e.BTN_MODE,
    "R_STK": e.BTN_THUMBL,
    "GR": e.BTN_C,
}

# Solo Left Joy-Con: SDL's HandleMiniControllerStateL (same Valve-contributed
# source as the right-side reference) treats this side's UP/DOWN/LEFT/RIGHT
# as face buttons, not a D-pad -- confirmed on hardware: using the D-pad hat
# here instead causes cross-talk where pressing a shoulder button registers
# as a direction. So face-button routing stays (SwitchGamepad skips the hat
# entirely for this product; see face_button_dpad). L/ZL are SDL's own
# "paddle" type there, but the closest evdev equivalent
# (BTN_TRIGGER_HAPPY1/2) broke Steam's device recognition wholesale when
# tried on the right side's R/ZR, so L/ZL use plain BTN_TL2/TR2 instead.
# SL_L/SR_L on TL/TR mirrors the right side's confirmed-correct SL_R/SR_R.
# A raw -v trace settled the confusing back-and-forth on directions: the
# SWITCH_BUTTONS UP/DOWN/LEFT/RIGHT bit *names* are fixed to the Joy-Con's
# printed (portrait/upright) orientation, not the landscape grip used for
# solo play -- e.g. physically pressing what's "up" when held sideways
# actually fires the "RIGHT" bit. So the four keys below are intentionally
# not lined up 1:1 with their names: each is the bit that fires for that
# *landscape* direction, mapped to the natural compass face-button
# (up=north, right=east, down=south, left=west).
JOYCON2_LEFT_BUTTON_MAP = {
    "RIGHT": e.BTN_WEST,   # landscape UP
    "DOWN": e.BTN_EAST,    # landscape RIGHT
    "LEFT": e.BTN_SOUTH,   # landscape DOWN
    "UP": e.BTN_NORTH,     # landscape LEFT
    "SR_L": e.BTN_TR,
    "SL_L": e.BTN_TL,
    "L": e.BTN_TL2,
    "ZL": e.BTN_TR2,
    "MINUS": e.BTN_SELECT,
    "CAPTURE": e.BTN_Z,
    "L_STK": e.BTN_THUMBL,
    "GL": e.BTN_C,
}

DEFAULT_BUTTON_MAP = PRO_BUTTON_MAP

# Joy-Con 2 (either side) presents a single physical stick; the uinput
# device drops the unused RX/RY axes accordingly (see SwitchGamepad). Both
# trigger axes (Z/RZ) and the D-pad hat are still declared even though only
# one side is ever real for a given product.
SINGLE_STICK_PIDS = {P.JOYCON2_LEFT_PID, P.JOYCON2_RIGHT_PID}


def button_map_for_product(product_id: int) -> dict:
    """Return the default evdev map for a Switch 2 controller PID."""
    if product_id == P.NSO_GAMECUBE_PID:
        return GAMECUBE_BUTTON_MAP
    if product_id == P.JOYCON2_LEFT_PID:
        return JOYCON2_LEFT_BUTTON_MAP
    if product_id == P.JOYCON2_RIGHT_PID:
        return JOYCON2_RIGHT_BUTTON_MAP
    return PRO_BUTTON_MAP


class SwitchGamepad:
    def __init__(
        self,
        name: str = "NSO GameCube Controller",
        button_map=None,
        product: int = P.NSO_GAMECUBE_PID,
        mac: str = "",
    ):
        self.button_map = button_map or DEFAULT_BUTTON_MAP
        self.product = product
        self.single_stick = product in SINGLE_STICK_PIDS
        # A lone Left Joy-Con has no real D-pad -- UP/DOWN/LEFT/RIGHT are
        # routed as face buttons in JOYCON2_LEFT_BUTTON_MAP instead (matches
        # SDL's own HandleMiniControllerStateL). Emitting a hat here too, on
        # hardware, caused shoulder presses to register as D-pad directions.
        self.face_button_dpad = product == P.JOYCON2_LEFT_PID
        keys = sorted(set(self.button_map.values()))

        abs_caps = [
            (e.ABS_X, AbsInfo(0, STICK_MIN, STICK_MAX, 0, 0, 0)),
            (e.ABS_Y, AbsInfo(0, STICK_MIN, STICK_MAX, 0, 0, 0)),
        ]
        if not self.single_stick:
            abs_caps += [
                (e.ABS_RX, AbsInfo(0, STICK_MIN, STICK_MAX, 0, 0, 0)),
                (e.ABS_RY, AbsInfo(0, STICK_MIN, STICK_MAX, 0, 0, 0)),
            ]
        # Both trigger axes are always declared, even for single-stick Joy-Con
        # products with only one real trigger: Steam's device-details screen
        # reads a Joy-Con's ZR status from ABS_RZ specifically and shows no
        # field at all for it if the axis isn't declared.
        abs_caps.append((e.ABS_Z, AbsInfo(0, TRIGGER_MIN, TRIGGER_MAX, 0, 0, 0)))
        abs_caps.append((e.ABS_RZ, AbsInfo(0, TRIGGER_MIN, TRIGGER_MAX, 0, 0, 0)))
        if not self.face_button_dpad:
            abs_caps += [
                (e.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)),
                (e.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0)),
            ]

        capabilities = {
            e.EV_KEY: keys,
            e.EV_ABS: abs_caps,
            e.EV_FF: [e.FF_RUMBLE, e.FF_PERIODIC, e.FF_CONSTANT, e.FF_GAIN],
        }

        phys = phys_for_mac(mac) if mac else "py-evdev-uinput"
        self.ui = UInput(
            capabilities,
            name=name,
            vendor=P.NINTENDO_VENDOR_ID,
            product=product,
            version=0x0100,
            bustype=e.BUS_BLUETOOTH,
            phys=phys,
        )
        logger.info("created virtual gamepad: %s", self.ui.device.path if self.ui.device else name)

        self._last_keys: dict[int, int] = {}
        self._last_abs: dict[int, int] = {}

        self.rumble_cb: Optional[Callable[[float, float], None]] = None
        self._effects: dict[int, tuple[int, int]] = {}
        self._ff_running = True
        self._ff_thread = threading.Thread(target=self._ff_loop, daemon=True)
        self._ff_thread.start()

    @staticmethod
    def _scale_stick(value: float) -> int:
        v = int(value * STICK_MAX)
        return max(STICK_MIN, min(STICK_MAX, v))

    def _emit_key(self, code: int, pressed: int) -> bool:
        if self._last_keys.get(code) != pressed:
            self.ui.write(e.EV_KEY, code, pressed)
            self._last_keys[code] = pressed
            return True
        return False

    def _emit_abs(self, code: int, value: int) -> bool:
        if self._last_abs.get(code) != value:
            self.ui.write(e.EV_ABS, code, value)
            self._last_abs[code] = value
            return True
        return False

    def update(
        self,
        buttons: int,
        left_stick: tuple[float, float],
        right_stick: tuple[float, float],
        left_trigger: int,
        right_trigger: int,
    ) -> None:
        changed = False

        # Emit every mapped key each frame; OR switch names that share a code
        # (GC R trigger click and Z both use Shoulder R / BTN_TR).
        key_states = {code: 0 for code in set(self.button_map.values())}
        for switch_name, key_code in self.button_map.items():
            mask = P.SWITCH_BUTTONS.get(switch_name, 0)
            if mask and (buttons & mask):
                key_states[key_code] = 1
        for key_code, pressed in key_states.items():
            changed |= self._emit_key(key_code, pressed)

        if not self.face_button_dpad:
            dpad_x = (1 if buttons & P.SWITCH_BUTTONS["RIGHT"] else 0) - (
                1 if buttons & P.SWITCH_BUTTONS["LEFT"] else 0
            )
            dpad_y = (1 if buttons & P.SWITCH_BUTTONS["DOWN"] else 0) - (
                1 if buttons & P.SWITCH_BUTTONS["UP"] else 0
            )
            changed |= self._emit_abs(e.ABS_HAT0X, dpad_x)
            changed |= self._emit_abs(e.ABS_HAT0Y, dpad_y)

        changed |= self._emit_abs(e.ABS_X, self._scale_stick(left_stick[0]))
        changed |= self._emit_abs(e.ABS_Y, -self._scale_stick(left_stick[1]))
        if not self.single_stick:
            changed |= self._emit_abs(e.ABS_RX, self._scale_stick(right_stick[0]))
            changed |= self._emit_abs(e.ABS_RY, -self._scale_stick(right_stick[1]))
        # Triggers keep their own natural side (ZL->Z, ZR->RZ) even for
        # single-stick Joy-Con products -- unlike the stick, which is
        # deliberately re-routed to "primary/left" in device.calibrated_input.
        changed |= self._emit_abs(e.ABS_Z, max(0, min(255, left_trigger)))
        changed |= self._emit_abs(e.ABS_RZ, max(0, min(255, right_trigger)))

        if changed:
            self.ui.syn()

    def release_all(self) -> None:
        changed = False
        for code in list(self._last_keys):
            changed |= self._emit_key(code, 0)
        neutral_codes = [e.ABS_X, e.ABS_Y, e.ABS_Z, e.ABS_RZ]
        if not self.single_stick:
            neutral_codes += [e.ABS_RX, e.ABS_RY]
        if not self.face_button_dpad:
            neutral_codes += [e.ABS_HAT0X, e.ABS_HAT0Y]
        for code in neutral_codes:
            changed |= self._emit_abs(code, 0)
        if changed:
            self.ui.syn()

    def _ff_loop(self) -> None:
        try:
            for event in self.ui.read_loop():
                if not self._ff_running:
                    break
                try:
                    self._handle_ff_event(event.type, event.code, event.value)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    def _handle_ff_event(self, etype: int, code: int, value: int) -> None:
        if etype == e.EV_UINPUT and code == e.UI_FF_UPLOAD:
            upload = self.ui.begin_upload(value)
            effect = upload.effect
            if effect.type == e.FF_RUMBLE:
                r = effect.u.ff_rumble_effect
                self._effects[effect.id] = (r.strong_magnitude, r.weak_magnitude)
            upload.retval = 0
            self.ui.end_upload(upload)
        elif etype == e.EV_UINPUT and code == e.UI_FF_ERASE:
            erase = self.ui.begin_erase(value)
            self._effects.pop(erase.effect_id, None)
            erase.retval = 0
            self.ui.end_erase(erase)
        elif etype == e.EV_FF:
            if self.rumble_cb is None:
                return
            if value == 0:
                self.rumble_cb(0.0, 0.0)
            else:
                strong, weak = self._effects.get(code, (0, 0))
                self.rumble_cb(strong / 65535.0, weak / 65535.0)

    def close(self) -> None:
        self._ff_running = False
        if self.rumble_cb is not None:
            try:
                self.rumble_cb(0.0, 0.0)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.ui.close()
        except Exception:  # noqa: BLE001
            pass


GameCubeGamepad = SwitchGamepad
