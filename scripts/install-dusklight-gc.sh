#!/usr/bin/env bash
# Install Dusklight/Twilight Princess controller profiles for ngc virtual pads.
# Fixes Aurora's broken NSO GameCube defaults (Z on Back, swapped B/X).
# Also builds the evdev uniq shim so SDL can pair ngc IMU nodes for gyro aim.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PROJECT_DIR}/.venv312/bin/python"
if [ ! -x "$PY" ]; then
  PY=python3
fi

bash "$PROJECT_DIR/scripts/build-ngc-evdev-shim.sh"

UDEV_RULE_SRC="$PROJECT_DIR/system/udev/70-ngc-imu-uaccess.rules"
UDEV_RULE_DST="/etc/udev/rules.d/70-ngc-imu-uaccess.rules"
if [ -f "$UDEV_RULE_SRC" ]; then
  if sudo cp "$UDEV_RULE_SRC" "$UDEV_RULE_DST" 2>/dev/null; then
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=input --action=add 2>/dev/null || true
    echo "installed $UDEV_RULE_DST (IMU session ACL for SDL gyro)"
  else
    echo "note: run once with sudo to install IMU uaccess rule:"
    echo "  sudo cp $UDEV_RULE_SRC $UDEV_RULE_DST && sudo udevadm control --reload-rules"
  fi
fi

WRAPPER="$HOME/.local/bin/dusklight-ngc-gyro"
install -m 0755 "$PROJECT_DIR/scripts/dusklight-with-ngc-gyro.sh" "$WRAPPER"
echo "installed $WRAPPER"

DUSK_DIR="${HOME}/.local/share/TwilitRealm/Dusklight"
if [ ! -d "$DUSK_DIR" ]; then
  echo "skip Dusklight profiles (not installed: $DUSK_DIR missing)"
  echo "gyro shim ready — use $WRAPPER or LD_PRELOAD when Dusklight is installed"
  exit 0
fi

"$PY" "$PROJECT_DIR/tools/dusklight_controller_profile.py" --cleanup --dir "$DUSK_DIR"

if ! "$PY" "$PROJECT_DIR/tools/dusklight_controller_profile.py" --auto --dir "$DUSK_DIR"; then
  "$PY" "$PROJECT_DIR/tools/dusklight_controller_profile.py" --dir "$DUSK_DIR"
fi

DESKTOP_SRC="$HOME/.local/share/applications/dusklight.desktop"
DESKTOP_OVR="$HOME/.local/share/applications/dusklight-ngc-gyro.desktop"
SHIM="$HOME/.local/lib/libngc_evdev_uniq.so"
if [ -f "$DESKTOP_SRC" ]; then
  ts() { date +%Y%m%d%H%M%S; }
  backup="$DESKTOP_SRC.bak.$(ts)"
  cp -p "$DESKTOP_SRC" "$backup"
  base_exec="$(grep -E '^Exec=' "$backup" | head -1 | sed 's/^Exec=//')"
  base_icon="$(grep -E '^Icon=' "$backup" | head -1 | sed 's/^Icon=//' || echo dusklight)"
  base_exec="${base_exec#env LD_PRELOAD=$SHIM }"
  base_exec="${base_exec#env LD_PRELOAD=$HOME/.local/lib/libngc_evdev_uniq.so }"
  patched_exec="env LD_PRELOAD=$SHIM $base_exec"
  awk -v exec="$patched_exec" '
    BEGIN { done = 0 }
    /^Exec=/ { if (!done) { print "Exec=" exec; done = 1; next } }
    { print }
  ' "$backup" > "$DESKTOP_SRC"
  echo "patched $DESKTOP_SRC (LD_PRELOAD for ngc gyro)"

  cat > "$DESKTOP_OVR" <<EOF
[Desktop Entry]
Type=Application
Name=Dusklight (ngc gyro)
Comment=Twilight Princess with ngc GameCube gyro aim
Exec=$patched_exec
Icon=$base_icon
Categories=Game;
Terminal=false
EOF
  echo "installed $DESKTOP_OVR"
fi

echo "Dusklight GameCube profiles installed (restart Dusklight if it is running)"
echo "Gyro: patched desktop + $WRAPPER use LD_PRELOAD; IMU node also needs one-time sudo udev rule (see above)"
