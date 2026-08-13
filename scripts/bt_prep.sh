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

# Steam owns a permanent discovery session; StopDiscovery can't clear it.
# btmgmt stop-find -b stops the BR/EDR Inquiry (the real interference -- see
# ngc/bridge.py _force_bt_inquiry_off) at the HCI layer. Also stop-find -l
# (LE scan) so Bazzite environments that leave an LE discovery session
# running (e.g. decky-bluetooth-wake-control racing with Steam Input) don't
# keep grabbing the adapter right after we clear BR/EDR discovery.
_btmgmt_ok=false
if sudo -n btmgmt -i 0 stop-find -b >/dev/null 2>&1; then
  echo "BR/EDR Inquiry stopped (btmgmt stop-find -b)"
  _btmgmt_ok=true
else
  echo "warn: btmgmt stop-find -b failed" >&2
fi

if [[ "$_btmgmt_ok" == "true" ]]; then
  if sudo -n btmgmt -i 0 stop-find -l >/dev/null 2>&1; then
    echo "LE scan stopped (btmgmt stop-find -l)"
  else
    echo "warn: btmgmt stop-find -l failed" >&2
  fi
fi

# Fallback: if btmgmt didn't work, use bluetoothctl power toggle.
# Some Bazzite builds have a btmgmt that rejects stop-find; power off/on
# reliably clears discovery without a full service restart.
if [[ "$_btmgmt_ok" != "true" ]]; then
  echo "fallback: power toggling adapter (bluetoothctl)"
  bluetoothctl power off >/dev/null 2>&1 || true
  sleep 0.5
  bluetoothctl power on >/dev/null 2>&1 || true
  sleep 1.0
fi

if [[ -n "$MAC" ]]; then
  bluetoothctl remove "$MAC" >/dev/null 2>&1 || true
fi
echo "Discovering: $(bluetoothctl show 2>/dev/null | grep -oE 'Discovering: (yes|no)' || echo unknown)"
