#!/usr/bin/env python3
"""Sync Eden controllers to whatever is connected right now.

Thin wrapper kept for launcher compatibility (eden-game.sh calls this).
The real logic lives in bazzite-controller-detect.py --eden, which rebinds
players 1-4 in Eden order (Pro Controller 2, DualSense, GC bridge, MCON,
EXLENE, N64) with per-pad button profiles and motion.

Old behavior (wiping every player and pinning P1 to one hardcoded profile)
forced manual controller setup on every launch — do not restore it.
"""
import os
import subprocess
import sys
from pathlib import Path

DETECTOR = Path.home() / ".local/bin/bazzite-controller-detect.py"


def main() -> int:
    if not DETECTOR.is_file():
        print(f"missing {DETECTOR}", file=sys.stderr)
        return 1
    env = dict(os.environ)
    # Eden gets physical pads passed through (native rumble/gyro); never bind
    # the Steam virtual X360 pads even when launched from Steam.
    env.setdefault("BAZZITE_STEAM_INPUT_FALLBACK", "never")
    proc = subprocess.run(
        [sys.executable, str(DETECTOR), "--eden"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
