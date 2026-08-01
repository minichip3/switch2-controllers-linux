"""Companion evdev IMU node for SDL3 / Dusklight gyro aim.

Dusklight reads motion via ``SDL_GamepadHasSensor`` (see Aurora ``pad.cpp``).
Our main uinput gamepad only exposes buttons and sticks, so SDL never sees a
gyroscope. Real Switch / DualSense pads expose a second evdev node marked
``INPUT_PROP_ACCELEROMETER``; SDL links it to the gamepad when both share the
same ``uniq``. uinput cannot set uniq (``EVIOCGUNIQ`` returns ENOENT), but both
nodes share ``phys`` (``ngc/MAC``). Install ``libngc_evdev_uniq.so`` and launch
Dusklight with ``LD_PRELOAD`` (see ``scripts/install-dusklight-gc.sh``).

Values follow SDL's linux joystick sensor scaling (``resolution`` field):
  accel output = value * g / resolution   (resolution = 4096, 1 LSB = 1/4096 g)
  gyro  output = value * pi/180 / resolution (resolution = 164, ~16.4 LSB per deg/s)
"""

from __future__ import annotations

import logging
from typing import Optional

from evdev import ecodes as e
from evdev import _uinput

from . import protocol as P

logger = logging.getLogger(__name__)

# Match ngc/dsu.py IMU scaling (LSM6DS3 full-scale defaults).
ACCEL_LSB_PER_G = 4096
GYRO_LSB_PER_DPS = 164  # 16.4 * 10 for integer resolution field

IMU_MIN, IMU_MAX = -32768, 32767

_ABS_AXES = (
    (e.ABS_X, ACCEL_LSB_PER_G),
    (e.ABS_Y, ACCEL_LSB_PER_G),
    (e.ABS_Z, ACCEL_LSB_PER_G),
    (e.ABS_RX, GYRO_LSB_PER_DPS),
    (e.ABS_RY, GYRO_LSB_PER_DPS),
    (e.ABS_RZ, GYRO_LSB_PER_DPS),
)


def phys_for_mac(mac: str) -> str:
    return f"ngc/{mac.upper()}"


class MotionEvdev:
    """Virtual accelerometer + gyroscope evdev node paired with a gamepad."""

    def __init__(self, name: str, mac: str, product: int = P.NSO_GAMECUBE_PID):
        self.mac = mac.upper()
        self._fd: Optional[int] = None
        self._last: dict[int, int] = {}

        phys = phys_for_mac(self.mac)
        imu_name = f"{name} IMU"

        fd = _uinput.open("/dev/uinput")
        _uinput.set_phys(fd, phys)
        _uinput.set_prop(fd, e.INPUT_PROP_ACCELEROMETER)
        for code, _res in _ABS_AXES:
            _uinput.enable(fd, e.EV_ABS, code)

        absinfo = [
            [code, 0, IMU_MIN, IMU_MAX, 0, 0, res]
            for code, res in _ABS_AXES
        ]
        _uinput.setup(
            fd,
            imu_name,
            P.NINTENDO_VENDOR_ID,
            product,
            0x0100,
            e.BUS_BLUETOOTH,
            absinfo,
            0,
        )
        _uinput.create(fd)
        self._fd = fd
        logger.info("created motion evdev: %s (phys %s)", imu_name, phys)

    def _emit(self, code: int, value: int) -> None:
        if self._fd is None:
            return
        value = max(IMU_MIN, min(IMU_MAX, int(value)))
        if self._last.get(code) == value:
            return
        self._last[code] = value
        _uinput.write(self._fd, e.EV_ABS, code, value)

    def update(self, report: P.InputReport) -> None:
        ax, ay, az = report.accel
        gx, gy, gz = report.gyro
        self._emit(e.ABS_X, ax)
        self._emit(e.ABS_Y, ay)
        self._emit(e.ABS_Z, az)
        self._emit(e.ABS_RX, gx)
        self._emit(e.ABS_RY, gy)
        self._emit(e.ABS_RZ, gz)
        if self._fd is not None:
            _uinput.write(self._fd, e.EV_SYN, e.SYN_REPORT, 0)

    def close(self) -> None:
        if self._fd is not None:
            try:
                _uinput.close(self._fd)
            except Exception:  # noqa: BLE001
                pass
            self._fd = None
            self._last.clear()
