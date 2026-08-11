#!/usr/bin/env bash
# Prepare BlueZ so a raw L2CAP connection can be initiated:
#  - stop the Decky "BT Wake Control" background scan (if present)
#  - stop bluetoothctl / BlueZ discovery sessions we own
#  - force HCI LE scan off (Steam keeps Discovering=yes otherwise)
#  - optionally forget a controller so BlueZ won't auto-grab it
# Safe to run repeatedly. Prefer passwordless sudo for btmgmt (Bazzite default).
set -u

MAC="${1:-}"

pkill -f decky-bluetooth-wake-control 2>/dev/null && echo "stopped decky bt-wake scan" || true
bluetoothctl scan off >/dev/null 2>&1 || true
busctl call org.bluez /org/bluez/hci0 org.bluez.Adapter1 StopDiscovery >/dev/null 2>&1 || true

# Steam owns a permanent discovery session that re-triggers the BR/EDR
# Inquiry ~1s after we stop it; disable Steam's own Bluetooth discovery so
# it stops contending (config.vdf patch, idempotent, timestamped backup).
if python3 "$(cd "$(dirname "$0")" && pwd)/disable-steam-bluetooth.py" 2>/dev/null | grep -q "disabled Steam Bluetooth"; then
  echo "Steam Bluetooth discovery disabled (config.vdf patched)"
fi

# Steam owns a permanent discovery session; StopDiscovery can't clear it.
# btmgmt stop-find -b stops the BR/EDR Inquiry (the real interference -- see
# ngc/bridge.py _force_bt_inquiry_off) at the HCI layer.
if sudo -n btmgmt -i 0 stop-find -b >/dev/null 2>&1; then
  echo "BR/EDR Inquiry stopped (btmgmt stop-find -b)"
else
  echo "warn: could not run 'sudo -n btmgmt -i 0 stop-find -b' (needed for reliable S2 connects)" >&2
fi

if [[ -n "$MAC" ]]; then
  bluetoothctl remove "$MAC" >/dev/null 2>&1 || true
fi
sleep 0.2
echo "Discovering: $(bluetoothctl show 2>/dev/null | grep -oE 'Discovering: (yes|no)' || echo unknown)"
