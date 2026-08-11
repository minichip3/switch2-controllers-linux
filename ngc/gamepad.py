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

# Solo Joy-Con 2 (Left). Every capability-minimizing approach tried before
# this (declare only what the Left half physically has, in kernel
# hid-nintendo's own evdev order) read correctly in Dolphin's raw evdev
# view but got scrambled in Steam -- confirmed on real hardware, via
# ~/.local/share/Steam/logs/controller.txt, that Steam has its own
# built-in vendor-level special-casing for vendor 0x057e (Nintendo) that
# forces a fixed "full Nintendo gamepad" capability *shape* and slot
# layout onto any 057e device, regardless of what it actually declares or
# what gamecontrollerdb.txt says. Fighting that (a neutral vendor ID)
# works but throws away Steam's native Joy-Con glyphs/labels for nothing.
#
# This instead leans into it: declares the full 15-code BTN_GAMEPAD block
# (same shape PRO_BUTTON_MAP/Pro Controller 2 already uses, which is
# exactly why Pro Controller 2 never needed any of this) so evdev's sorted
# button indices land on the *exact* slots Steam's forced template expects
# (reverse-engineered from controller.txt's logged mapping for vendor
# 057e): b6=leftshoulder, b7=rightshoulder, b10=back, b11=start,
# b13=leftstick, dpad=Hat0X/Y. Everything else in the block (b0/1/3/4 face
# buttons, b12 guide, b14 rightstick -- Left Joy-Con has none of these) is
# declared but deliberately never driven, so it just sits dead the same
# way it does on a real solo Joy-Con read through Steam's own native path.
#
# SL/SR reuse the block's "right side" shoulder slots (opposite-side reuse,
# same idea as the kernel driver's own convention) since Left only has one
# real left-side shoulder pair (L/ZL) but the block offers two full pairs.
# ZL and SR don't get slots here at all -- b8/b9 (TL2/TR2) aren't
# referenced by Steam's forced template (dead regardless of what's on
# them), so they're driven as the *trigger axes* the template does use
# (lefttrigger/righttrigger) instead -- see _DIGITAL_TRIGGER_AXES below.
# CAPTURE goes to start (b11), not guide/BTN_MODE (b12) -- a real button on
# BTN_MODE previously caused Steam to hijack the mouse cursor (see this
# project's history), and b12 isn't needed for anything else here.
JOYCON2_LEFT_BUTTON_MAP = {
    "L": e.BTN_TL,
    "SL_L": e.BTN_TR,
    "MINUS": e.BTN_SELECT,
    "CAPTURE": e.BTN_START,
    "L_STK": e.BTN_THUMBL,
}
# D-pad goes out as Hat0X/Y (SwitchGamepad's existing hardcoded hat
# emission, not this map -- see _DPAD_AS_BUTTONS below), matching Steam's
# forced template (dpup/dpdown/dpleft/dpright:h0.*), not the discrete
# BTN_DPAD_* buttons the kernel-convention approach used before.

# Dead filler capabilities that exist purely to keep JOYCON2_LEFT_BUTTON_MAP's
# real buttons landing on the right evdev-sorted indices (see the big
# comment above) -- Left Joy-Con has none of these and they're never
# written to.
_JOYCON2_LEFT_SHAPE_FILLER_KEYS = (
    e.BTN_SOUTH,  # b0 a
    e.BTN_EAST,   # b1 b
    e.BTN_C,      # b2 (unused by Steam's template)
    e.BTN_WEST,   # b3 x
    e.BTN_NORTH,  # b4 y
    e.BTN_Z,      # b5 (unused by Steam's template)
    e.BTN_TL2,    # b8 (unused by Steam's template -- ZL is a trigger axis instead)
    e.BTN_TR2,    # b9 (unused by Steam's template -- SR is a trigger axis instead)
    e.BTN_MODE,   # b12 guide -- deliberately left dead, see CAPTURE comment above
    e.BTN_THUMBR, # b14 rightstick -- no right stick to click
)

# ZL/SR (BLE digital buttons) simulated as the analog trigger axes Steam's
# forced template actually reads (lefttrigger:a2, righttrigger:a5) --
# jumps between 0 and full-scale on press/release rather than a real analog
# sweep, since there's no real analog sensor behind either button.
_DIGITAL_TRIGGER_AXES = {
    P.JOYCON2_LEFT_PID: {"ZL": e.ABS_Z, "SR_L": e.ABS_RZ},
}

# Tried advertising the real Nintendo Switch (1) Joy-Con (L) USB product ID
# (0x2006) here instead of the actual Switch 2 PID, on the theory that SDL's
# built-in Joy-Con recognition would kick in for free. Backfired on real
# hardware: SDL's evdev/joystick backend specifically excludes VID/PIDs it
# knows have a dedicated HIDAPI driver (SDL_JOYSTICK_HIDAPI_SWITCH covers
# 0x057e:0x2006) from the generic path entirely, assuming HIDAPI already
# has it covered -- but HIDAPI enumerates real hidraw nodes, which this
# uinput device doesn't have, so posing as that PID made the device
# disappear from SDL completely instead of getting labeled better. Back to
# the real Switch 2 protocol PID; the explicit gamecontrollerdb line in
# tools/sdl_guid.py is what actually gets Steam a correct mapping.
def uinput_product_id(product: int) -> int:
    """Product ID to advertise over uinput -- currently always the real
    BLE/protocol product ID; kept as a seam in case a product ever needs to
    diverge again."""
    return product


# Tried bumping the uinput version (bcdDevice) for this product to force a
# new SDL GUID, on the theory Steam was reusing a cached mapping keyed by
# the old one. Ruled out on real hardware: controller.txt showed Steam
# apply the exact same (wrong) auto-guessed mapping even for the brand new,
# never-before-seen GUID -- not a caching issue after all (see
# uinput_vendor_id() below for what actually was). Left in place
# since it's harmless and does still get a fresh GUID for this pad.
_UINPUT_VERSION_OVERRIDES = {P.JOYCON2_LEFT_PID: 0x0101}


def uinput_version(product: int) -> int:
    return _UINPUT_VERSION_OVERRIDES.get(product, 0x0100)


# Tried a neutral, non-Nintendo vendor ID (0x1209, pid.codes) for this pad
# to dodge Steam's vendor-057e special-casing entirely. Works, but throws
# away Steam's native Joy-Con recognition/glyphs for nothing -- superseded
# by leaning into that special-casing instead (see
# JOYCON2_LEFT_BUTTON_MAP's doc comment and _JOYCON2_LEFT_SHAPE_FILLER_KEYS
# below), which needs the real vendor ID to actually trigger.
def uinput_vendor_id(product: int) -> int:
    return P.NINTENDO_VENDOR_ID


DEFAULT_BUTTON_MAP = PRO_BUTTON_MAP


def button_map_for_product(product_id: int) -> dict:
    """Return the default evdev map for a Switch 2 controller PID."""
    if product_id == P.NSO_GAMECUBE_PID:
        return GAMECUBE_BUTTON_MAP
    if product_id == P.JOYCON2_LEFT_PID:
        return JOYCON2_LEFT_BUTTON_MAP
    return PRO_BUTTON_MAP


class SwitchGamepad:
    # Products that declare a right-stick pair (ABS_RX/RY). Solo Left
    # Joy-Con is here now too, but faked/always-neutral -- see
    # _FAKE_RIGHT_STICK -- purely so its capability shape matches what
    # Steam's vendor-057e special-casing expects (see
    # JOYCON2_LEFT_BUTTON_MAP's doc comment).
    _HAS_RIGHT_STICK = {P.PRO_CONTROLLER2_PID, P.NSO_GAMECUBE_PID, P.JOYCON2_RIGHT_PID, P.JOYCON2_LEFT_PID}
    # Solo Left Joy-Con has no real right stick to read -- always emit
    # neutral (0, 0) regardless of whatever bridge.py happens to pass as
    # right_stick for it (there's no real sensor behind that data).
    _FAKE_RIGHT_STICK = {P.JOYCON2_LEFT_PID}
    # GC: real analog L/R. Solo Left Joy-Con: ZL/SR simulated as digital
    # 0/full-scale jumps -- see _DIGITAL_TRIGGER_AXES.
    _HAS_TRIGGER_AXES = {P.NSO_GAMECUBE_PID, P.JOYCON2_LEFT_PID}
    _DPAD_AS_BUTTONS: set[int] = set()  # nothing uses discrete BTN_DPAD_* anymore; all pads use Hat0X/Y

    def __init__(
        self,
        name: str = "NSO GameCube Controller",
        button_map=None,
        product: int = P.NSO_GAMECUBE_PID,
        mac: str = "",
    ):
        self.button_map = button_map or DEFAULT_BUTTON_MAP
        keys = set(self.button_map.values())
        keys.update(_JOYCON2_LEFT_SHAPE_FILLER_KEYS if product == P.JOYCON2_LEFT_PID else ())
        self._digital_trigger_axes = _DIGITAL_TRIGGER_AXES.get(product, {})
        self._fake_right_stick = product in self._FAKE_RIGHT_STICK
        # SDL's evdev joystick classifier (SDL_EVDEV_GuessDeviceClass) needs
        # ABS_X/Y plus BTN_TRIGGER/BTN_A/BTN_1 or a right-stick-ish axis --
        # solo Left Joy-Con now declares BTN_SOUTH (BTN_A) as one of
        # _JOYCON2_LEFT_SHAPE_FILLER_KEYS and a (faked) right stick, so it
        # satisfies this on its own; no separate BTN_TRIGGER hint needed
        # anymore.
        keys = sorted(keys)

        self._has_right_stick = product in self._HAS_RIGHT_STICK
        self._has_trigger_axes = product in self._HAS_TRIGGER_AXES
        self._dpad_as_buttons = product in self._DPAD_AS_BUTTONS
        # The Left Joy-Con's stick reports its raw axes rotated 90 degrees
        # clockwise from what a standalone stick should read (pushing left
        # registered as up) -- confirmed on real hardware. Rotating the
        # input 90 degrees counter-clockwise before scaling, (x, y) -> (-y, x),
        # cancels it out. Not needed solo-Right or paired, where the stick
        # reads correctly already.
        self._rotate_left_stick_ccw90 = product == P.JOYCON2_LEFT_PID

        abs_axes = [
            (e.ABS_X, AbsInfo(0, STICK_MIN, STICK_MAX, 0, 0, 0)),
            (e.ABS_Y, AbsInfo(0, STICK_MIN, STICK_MAX, 0, 0, 0)),
        ]
        if not self._dpad_as_buttons:
            abs_axes += [
                (e.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)),
                (e.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0)),
            ]
        if self._has_right_stick:
            abs_axes += [
                (e.ABS_RX, AbsInfo(0, STICK_MIN, STICK_MAX, 0, 0, 0)),
                (e.ABS_RY, AbsInfo(0, STICK_MIN, STICK_MAX, 0, 0, 0)),
            ]
        if self._has_trigger_axes:
            abs_axes += [
                (e.ABS_Z, AbsInfo(0, TRIGGER_MIN, TRIGGER_MAX, 0, 0, 0)),
                (e.ABS_RZ, AbsInfo(0, TRIGGER_MIN, TRIGGER_MAX, 0, 0, 0)),
            ]

        capabilities = {
            e.EV_KEY: keys,
            e.EV_ABS: abs_axes,
            e.EV_FF: [e.FF_RUMBLE, e.FF_PERIODIC, e.FF_CONSTANT, e.FF_GAIN],
        }

        phys = phys_for_mac(mac) if mac else "py-evdev-uinput"
        self.ui = UInput(
            capabilities,
            name=name,
            vendor=uinput_vendor_id(product),
            product=uinput_product_id(product),
            version=uinput_version(product),
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

        if not self._dpad_as_buttons:
            dpad_x = (1 if buttons & P.SWITCH_BUTTONS["RIGHT"] else 0) - (
                1 if buttons & P.SWITCH_BUTTONS["LEFT"] else 0
            )
            dpad_y = (1 if buttons & P.SWITCH_BUTTONS["DOWN"] else 0) - (
                1 if buttons & P.SWITCH_BUTTONS["UP"] else 0
            )
            changed |= self._emit_abs(e.ABS_HAT0X, dpad_x)
            changed |= self._emit_abs(e.ABS_HAT0Y, dpad_y)

        lx, ly = left_stick
        if self._rotate_left_stick_ccw90:
            lx, ly = -ly, lx
        changed |= self._emit_abs(e.ABS_X, self._scale_stick(lx))
        changed |= self._emit_abs(e.ABS_Y, -self._scale_stick(ly))
        if self._has_right_stick:
            rx, ry = (0.0, 0.0) if self._fake_right_stick else right_stick
            changed |= self._emit_abs(e.ABS_RX, self._scale_stick(rx))
            changed |= self._emit_abs(e.ABS_RY, -self._scale_stick(ry))
        if self._has_trigger_axes:
            if self._digital_trigger_axes:
                for switch_name, axis_code in self._digital_trigger_axes.items():
                    mask = P.SWITCH_BUTTONS.get(switch_name, 0)
                    value = 255 if (mask and buttons & mask) else 0
                    changed |= self._emit_abs(axis_code, value)
            else:
                changed |= self._emit_abs(e.ABS_Z, max(0, min(255, left_trigger)))
                changed |= self._emit_abs(e.ABS_RZ, max(0, min(255, right_trigger)))

        if changed:
            self.ui.syn()

    def release_all(self) -> None:
        changed = False
        for code in list(self._last_keys):
            changed |= self._emit_key(code, 0)
        neutral_axes = [(e.ABS_X, 0), (e.ABS_Y, 0)]
        if not self._dpad_as_buttons:
            neutral_axes += [(e.ABS_HAT0X, 0), (e.ABS_HAT0Y, 0)]
        if self._has_right_stick:
            neutral_axes += [(e.ABS_RX, 0), (e.ABS_RY, 0)]
        if self._has_trigger_axes:
            neutral_axes += [(e.ABS_Z, 0), (e.ABS_RZ, 0)]
        for code, neutral in neutral_axes:
            changed |= self._emit_abs(code, neutral)
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
