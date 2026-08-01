#!/usr/bin/env bash
# Build libngc_evdev_uniq.so for SDL/Dusklight gyro pairing on uinput ngc pads.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$PROJECT_DIR/system/ngc_evdev_uniq_shim.c"
LIB_DIR="${NGC_SHIM_DIR:-$HOME/.local/lib}"
OUT="$LIB_DIR/libngc_evdev_uniq.so"

mkdir -p "$LIB_DIR"
cc -shared -fPIC -O2 -Wall -Wextra -o "$OUT" "$SRC" -ldl
echo "built $OUT"
