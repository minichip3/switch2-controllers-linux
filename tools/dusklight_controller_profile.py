#!/usr/bin/env python3
"""Install corrected Dusklight (Twilight Princess) .controller profiles for ngc pads.

Dusklight/Aurora ships broken defaults for NSO GameCube (PID 0x2073): Z-target on
Back/Capture and swapped B/X. Our ngc uinput bridge uses the standard SDL layout
(A=SOUTH, B=EAST, X=WEST, Y=NORTH, Z=RIGHT_SHOULDER, L/R on trigger axes) — the
same mapping Aurora uses for Xbox/PS5/Pro controllers.

This writes that standard profile into ~/.local/share/TwilitRealm/Dusklight/
so Dusklight stops loading the bad NSO preset from stale .controller files.
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

# Aurora CTRL v4 profile: g_defaultButtonsStandard + g_defaultAxes (from a working
# PS5 profile on Bazzite — identical mapping Dusklight expects for our uinput pad).
_STANDARD_PROFILE_B64 = (
    "Q1RSTAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAUAfQB+uea55AAAAAAABAAABAAAAAAI"
    "AAAIAAAAABAAAAwAAAAAIAAAGAAAAABAAAAoAAAAQAAAA/////0AAAAD/////IAAAAAsAAAAI"
    "AAAADAAAAAQAAAANAAAAAQAAAA4AAAACAAAAAAAAAAEAAAD/////AAAAAAAAAAD//////////wEA"
    "AAABAAAA//////////8CAAAAAQAAAAEAAAD/////AwAAAAIAAAABAAAA/////wQAAAACAAAA"
    "//////////8FAAAAAwAAAP//////////BgAAAAMAAAABAAAA/////wcAAAAEAAAAAQAAAP////8I"
    "AAAABQAAAAEAAAD/////CQAAAP9//38A"
)

DUSKLIGHT_DIR = Path.home() / ".local/share/TwilitRealm/Dusklight"
NINTENDO_VID = 0x057E
NSO_GC_PID = 0x2073
PRO2_PID = 0x2069


def build_profile() -> bytes:
    data = base64.b64decode(_STANDARD_PROFILE_B64)
    if len(data) != 303:
        raise RuntimeError(f"profile template size mismatch: {len(data)}")
    return data


def profile_path(name: str, vid: int, pid: int, base: Path = DUSKLIGHT_DIR) -> Path:
    return base / f"{name}_{vid:04X}_{pid:04X}.controller"


def install_for_name(name: str, vid: int, pid: int, base: Path = DUSKLIGHT_DIR, *, dry_run: bool = False) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    path = profile_path(name, vid, pid, base)
    data = build_profile()
    if dry_run:
        print(f"would write {path} ({len(data)} bytes)")
        return path
    path.write_bytes(data)
    print(f"wrote {path}")
    return path


def _should_refresh(path: Path) -> bool:
    name = path.name.lower()
    if "_057e_2073.controller" not in name and "_057e_2069.controller" not in name:
        return False
    return (
        "gamecube" in name
        or "pro controller 2" in name
        or "switch 2 pro" in name
    )


def refresh_existing(base: Path = DUSKLIGHT_DIR, *, dry_run: bool = False) -> list[Path]:
    data = build_profile()
    written: list[Path] = []
    for path in sorted(base.glob("*_057E_*.controller")):
        if not _should_refresh(path):
            continue
        if dry_run:
            print(f"would refresh {path}")
        else:
            path.write_bytes(data)
            print(f"refreshed {path}")
        written.append(path)
    return written


def cleanup_stale(base: Path = DUSKLIGHT_DIR, *, dry_run: bool = False) -> None:
    """Remove wrong-PID NSO profiles (e.g. 057E:2069 saved before product fix)."""
    for path in base.glob("*GameCube*_057E_2069.controller"):
        if dry_run:
            print(f"would remove stale {path}")
        else:
            path.unlink(missing_ok=True)
            print(f"removed stale {path}")


def autodetect_from_sdl(base: Path = DUSKLIGHT_DIR, *, dry_run: bool = False) -> list[Path]:
    try:
        import sdl2
    except ImportError:
        return []

    sdl2.SDL_Init(sdl2.SDL_INIT_GAMECONTROLLER | sdl2.SDL_INIT_JOYSTICK)
    written: list[Path] = []
    for i in range(sdl2.SDL_NumJoysticks()):
        name_b = sdl2.SDL_JoystickNameForIndex(i)
        if not name_b:
            continue
        name = name_b.decode()
        low = name.lower()
        if "gamecube" not in low and "pro controller 2" not in low:
            continue
        js = sdl2.SDL_JoystickOpen(i)
        if not js:
            continue
        vid = sdl2.SDL_JoystickGetVendor(js)
        pid = sdl2.SDL_JoystickGetProduct(js)
        sdl2.SDL_JoystickClose(js)
        if pid not in (NSO_GC_PID, PRO2_PID):
            continue
        written.append(install_for_name(name, vid, pid, base, dry_run=dry_run))
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", type=Path, default=DUSKLIGHT_DIR, help="Dusklight data directory")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--name", help='Exact SDL device name, e.g. "NSO GameCube Controller (P2)"')
    ap.add_argument("--vid", type=lambda x: int(x, 0), default=NINTENDO_VID)
    ap.add_argument("--pid", type=lambda x: int(x, 0), default=NSO_GC_PID)
    ap.add_argument("--auto", action="store_true", help="Install for connected ngc SDL gamepads")
    ap.add_argument("--cleanup", action="store_true", help="Remove stale wrong-PID profiles")
    args = ap.parse_args()

    if args.cleanup:
        cleanup_stale(args.dir, dry_run=args.dry_run)

    if args.auto:
        paths = autodetect_from_sdl(args.dir, dry_run=args.dry_run)
        if not paths:
            print("no NSO GameCube / Pro Controller 2 pads found via SDL", file=sys.stderr)
            return 1
        return 0

    if not args.name:
        paths = refresh_existing(args.dir, dry_run=args.dry_run)
        if not paths:
            ap.error("no GameCube / Pro Controller 2 .controller files found; use --auto or --name")
        return 0

    install_for_name(args.name, args.vid, args.pid, args.dir, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
