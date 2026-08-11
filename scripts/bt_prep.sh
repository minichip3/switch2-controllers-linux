#!/usr/bin/env bash
# Prepare BlueZ so a raw L2CAP connection can be initiated:
#  - stop the Decky "BT Wake Control" background scan (if present)
#  - stop bluetoothctl / BlueZ discovery sessions we own
#  - force HCI BR/EDR Inquiry off (Steam/bluetoothd keep Discovering=yes
#    otherwise)
#  - optionally forget a controller so BlueZ won't auto-grab it
# Safe to run repeatedly. Prefer passwordless sudo for btmgmt (Bazzite default).
#
# Real-hardware finding (see ngc/bridge.py's _force_bt_inquiry_off doc
# comment for the full story): the interference here is continuous BR/EDR
# Inquiry, not an LE scan -- `stop-find -l` against it always returned
# "Invalid Parameters" (a type mismatch) and never actually helped. `-b`
# is the flag that works, though this one-shot script only buys a brief
# window (bluetoothd resumes Inquiry within about a second) -- the bridge
# itself re-suppresses continuously for the whole connect handshake.
set -u

MAC="${1:-}"

pkill -f decky-bluetooth-wake-control 2>/dev/null && echo "stopped decky bt-wake scan" || true
bluetoothctl scan off >/dev/null 2>&1 || true
busctl call org.bluez /org/bluez/hci0 org.bluez.Adapter1 StopDiscovery >/dev/null 2>&1 || true

# Steam owns a permanent discovery session; StopDiscovery can't clear it.
# btmgmt stop-find -b stops BR/EDR Inquiry at the HCI layer.
if sudo -n btmgmt -i 0 stop-find -b >/dev/null 2>&1; then
  echo "HCI BR/EDR Inquiry stopped (btmgmt stop-find -b)"
else
  echo "warn: could not run 'sudo -n btmgmt -i 0 stop-find -b' (needed for reliable S2 connects)" >&2
fi

if [[ -n "$MAC" ]]; then
  bluetoothctl remove "$MAC" >/dev/null 2>&1 || true
fi
sleep 0.2
echo "Discovering: $(bluetoothctl show 2>/dev/null | grep -oE 'Discovering: (yes|no)' || echo unknown)"
