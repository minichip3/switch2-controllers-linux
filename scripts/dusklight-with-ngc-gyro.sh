#!/usr/bin/env bash
# Launch Dusklight with ngc IMU pairing shim (SDL needs uniq; uinput only has phys).
set -euo pipefail

SHIM="${NGC_EVDEV_UNIQ_SHIM:-$HOME/.local/lib/libngc_evdev_uniq.so}"
DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications/dusklight.desktop"
APPDIR="${DUSKLIGHT_APPDIR:-$HOME/Applications/Dusklight.AppDir}"

if [ ! -f "$SHIM" ]; then
  echo "missing $SHIM — run: ~/nso-gc-bazzite/scripts/build-ngc-evdev-shim.sh" >&2
  exit 1
fi

exec_cmd=""
if [ -f "$DESKTOP" ]; then
  exec_cmd="$(grep -E '^Exec=' "$DESKTOP" | head -1 | sed 's/^Exec=//' | sed 's/ %[fFuU]//g')"
fi
if [ -z "$exec_cmd" ] && [ -x "$APPDIR/AppRun" ]; then
  exec_cmd="$APPDIR/AppRun"
fi
if [ -z "$exec_cmd" ]; then
  echo "could not find Dusklight (no $DESKTOP Exec= or $APPDIR/AppRun)" >&2
  exit 1
fi

export LD_PRELOAD="${SHIM}${LD_PRELOAD:+:$LD_PRELOAD}"
exec bash -lc "$exec_cmd"
