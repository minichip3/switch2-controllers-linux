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

# Solo Joy-Con 2 (Left), used standalone rather than snapped to a grip or
# paired with its other half. Physically has no A/B/X/Y, right stick, PLUS,
# HOME, or analog triggers -- those live on the Right half or the grip --
# so this deliberately only covers what the Left half actually has, instead
# of reusing PRO_BUTTON_MAP (which declared all of those as dead
# capabilities and had no slot at all for SL/SR).
#
# Slot choices mirror the upstream kernel `hid-nintendo` driver's
# `left_joycon_button_mappings` (the "Joy-Con 1" convention) rather than
# Steam Input's Joy-Con-2-specific SDL profile: SL/SR reuse the *opposite*
# side's unused shoulder/trigger slots (SL->TR, SR->TR2) while this half's
# own L/ZL take the same-side slots (L->TL, ZL->TL2); MINUS->SELECT,
# CAPTURE->Z. D-pad is four discrete BTN_DPAD_* buttons, not a Hat0X/Y
# axis -- also matching what the real driver sends, confirmed against
# Steam's own device capability listing on real hardware (in exactly this
# order: Z, TL, TR, TL2, TR2, SELECT, THUMBL, DPAD_UP/DOWN/LEFT/RIGHT, then
# two plain stick axes, no hat).
JOYCON2_LEFT_BUTTON_MAP = {
    "CAPTURE": e.BTN_Z,
    "L": e.BTN_TL,
    "SL_L": e.BTN_TR,
    "ZL": e.BTN_TL2,
    "SR_L": e.BTN_TR2,
    "MINUS": e.BTN_SELECT,
    "L_STK": e.BTN_THUMBL,
    "UP": e.BTN_DPAD_UP,
    "DOWN": e.BTN_DPAD_DOWN,
    "LEFT": e.BTN_DPAD_LEFT,
    "RIGHT": e.BTN_DPAD_RIGHT,
}

# Solo Joy-Con 2 (Right). Same kernel hid-nintendo convention as
# JOYCON2_LEFT_BUTTON_MAP, confirmed against real hardware's Dolphin evdev
# capability listing (in exactly this sorted order: SOUTH, EAST, NORTH,
# WEST, TL, TR, TL2, TR2, START, MODE, THUMBR, then two plain stick axes).
# Unlike the Left half, the Right half's primary 4 buttons are real face
# buttons (A/B/X/Y), not a D-pad, so BTN_SOUTH ends up declared and driven
# on its own -- no dead filler capability needed to satisfy SDL's joystick
# classifier here, unlike JOYCON2_LEFT_BUTTON_MAP's BTN_A.
#
# SL/SR reuse the *opposite* (left) side's shoulder/trigger slots, same
# opposite-side-reuse idea as the Left half's SL/SR -- this side's own
# R/ZR take the same-side slots. Stick click is THUMBR here (not THUMBL --
# unlike the Left half, whose click lands on THUMBL because its single
# stick is presented as the primary/left stick regardless of side; see
# SwitchGamepad and device.calibrated_input's Right-Joy-Con stick-source
# swap. The *button* semantics stay side-correct even though the axis data
# doesn't).
JOYCON2_RIGHT_BUTTON_MAP = {
    "A": e.BTN_SOUTH,
    "B": e.BTN_EAST,
    "X": e.BTN_WEST,
    "Y": e.BTN_NORTH,
    "SL_R": e.BTN_TL,
    "R": e.BTN_TR,
    "SR_R": e.BTN_TL2,
    "ZR": e.BTN_TR2,
    "PLUS": e.BTN_START,
    "HOME": e.BTN_MODE,
    "R_STK": e.BTN_THUMBR,
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
# _UINPUT_VENDOR_OVERRIDES below for what actually was). Left in place
# since it's harmless and does still get a fresh GUID for this pad.
_UINPUT_VERSION_OVERRIDES = {P.JOYCON2_LEFT_PID: 0x0101}


def uinput_version(product: int) -> int:
    return _UINPUT_VERSION_OVERRIDES.get(product, 0x0100)


# The mapping Steam actually applied (per controller.txt) didn't match
# JOYCON2_LEFT_BUTTON_MAP's real capabilities *at all* -- 4 face buttons, a
# right stick, analog triggers, a d-pad hat -- none of which this device
# declares. That mismatch was identical across two different SDL GUIDs
# (ruling out a GUID-keyed cache) and identical to the generic "Nintendo
# Switch Pro Controller"-shaped template, which points at Steam having its
# own built-in vendor-level special-casing for vendor 0x057e (Nintendo)
# that overrides gamecontrollerdb.txt/evdev capabilities entirely, separate
# from -- and evaluated before -- the normal SDL_GameControllerDB path.
#
# Tried a neutral, non-Nintendo vendor ID (0x1209, pid.codes) to dodge that
# special-casing. Reverted: the plan is to bind everything by hand in
# Steam's own personalization UI anyway (rather than chase automatic
# recognition further), and for that the real Nintendo vendor ID -- an
# honest match for what this half-a-real-Joy-Con device actually is, same
# evdev capability layout the real kernel hid-nintendo driver sends -- is
# just as usable as a neutral one; whatever glyphs/icon Steam picks from
# the vendor ID don't matter once every input is bound manually anyway.
def uinput_vendor_id(product: int) -> int:
    return P.NINTENDO_VENDOR_ID


DEFAULT_BUTTON_MAP = PRO_BUTTON_MAP


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
    # Only the products in this set have a physical right stick / analog
    # trigger pair -- everything else (a solo Joy-Con half) declaring those
    # axes anyway just shows up as a permanently-centered phantom stick and
    # dead trigger axes in Dolphin/Steam's device inspector. Solo Right
    # Joy-Con's one stick is presented as the *primary* (left/ABS_X/Y)
    # stick regardless of side (see device.calibrated_input's stick-source
    # swap for it), so it has no second stick to declare here either.
    _HAS_RIGHT_STICK = {P.PRO_CONTROLLER2_PID, P.NSO_GAMECUBE_PID}
    _HAS_TRIGGER_AXES = {P.NSO_GAMECUBE_PID}  # only the GC pad has true analog L/R
    # Solo Left Joy-Con sends its D-pad as four discrete buttons (see
    # JOYCON2_LEFT_BUTTON_MAP), not a Hat0X/Y axis -- so it needs neither
    # the hat capability nor the hardcoded hat emission in update()/
    # release_all() below. Solo Right Joy-Con has no D-pad concept at all
    # (its primary 4 buttons are real A/B/X/Y face buttons instead) so it
    # doesn't need the hat either -- lands in this set for the same "skip
    # declaring/emitting Hat0X/Y" effect even though it has no D-pad-as-
    # buttons mapping of its own.
    _DPAD_AS_BUTTONS = {P.JOYCON2_LEFT_PID, P.JOYCON2_RIGHT_PID}
    # SDL's evdev joystick classifier (SDL_EVDEV_GuessDeviceClass, checked
    # against actual SDL source) only sets ID_INPUT_JOYSTICK if, besides
    # ABS_X/Y, the device has BTN_TRIGGER, BTN_A, BTN_1, or one of a handful
    # of ABS axes (RX/RY/RZ/throttle/rudder/wheel/gas/brake) -- everything
    # solo Left Joy-Con deliberately doesn't declare. A udev rule tagging
    # ID_INPUT_JOYSTICK directly (system/udev/72-ngc-joystick.rules) was
    # tried first instead of a dead capability here, on the theory that real
    # Joy-Cons never hit this classifier at all (they're recognized through
    # SDL's HIDAPI driver via raw VID/PID over hidraw, a completely
    # different path we can't reach from a uinput device) -- but that alone
    # didn't fix it on real hardware (Steam sandboxing/udev-database access
    # is the likely reason: this classifier is a *process-local* ioctl bit
    # check SDL does itself, not dependent on being able to read udev's
    # database at all, so it's the more reliable fix regardless of what
    # environment Steam is running in). Originally used BTN_TRIGGER (over
    # BTN_A) so a binding UI wouldn't show a stuck-at-0 face button --
    # reverted: BTN_TRIGGER lives in the separate BTN_JOYSTICK code block
    # (0x120-0x12f) instead of BTN_GAMEPAD (0x130+, where every other
    # button here lives), and having one button from that other block
    # apparently makes Dolphin's device list reclassify the whole pad as a
    # "generic joystick" and fall back to numbered "Button 1/2/3" labels
    # instead of symbolic ones (BTN_Z, BTN_TL, ...) for every button, not
    # just the odd one out -- confirmed on real hardware. BTN_A stays
    # inside the BTN_GAMEPAD block like everything else, so it doesn't
    # trip that reclassification; the one dead "A" entry in a binding UI
    # is the smaller cost.
    _NEEDS_SDL_JOYSTICK_HINT = {P.JOYCON2_LEFT_PID}

    def __init__(
        self,
        name: str = "NSO GameCube Controller",
        button_map=None,
        product: int = P.NSO_GAMECUBE_PID,
        mac: str = "",
    ):
        self.button_map = button_map or DEFAULT_BUTTON_MAP
        keys = set(self.button_map.values())
        if product in self._NEEDS_SDL_JOYSTICK_HINT:
            keys.add(e.BTN_A)
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

        # Left Joy-Con's D-pad already went out as BTN_DPAD_* above, via
        # button_map -- it has no Hat0X/Y capability to write to.
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
            changed |= self._emit_abs(e.ABS_RX, self._scale_stick(right_stick[0]))
            changed |= self._emit_abs(e.ABS_RY, -self._scale_stick(right_stick[1]))
        if self._has_trigger_axes:
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
