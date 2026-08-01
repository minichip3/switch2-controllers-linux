#!/usr/bin/env python3
"""Disable Steam's Bluetooth.Enabled so it stops perpetual LE discovery.

ngc needs the HCI LE scan free for raw L2CAP connects. DualSense and other
already-paired BlueZ devices keep working via the system stack.
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

p = Path.home() / ".steam/steam/config/config.vdf"
text = p.read_text(encoding="utf-8", errors="replace")
bak = p.with_name(f"config.vdf.bak-btfix-{int(time.time())}")
pat = re.compile(r'("Bluetooth"\s*\{\s*"Enabled"\s*")1(")', re.M)
new, n = pat.subn(r"\g<1>0\g<2>", text, count=1)
if n:
    shutil.copy2(p, bak)
    p.write_text(new, encoding="utf-8")
    print(f"disabled Steam Bluetooth.Enabled (backup {bak.name})")
else:
    m = re.search(r'"Bluetooth"\s*\{[^}]*\}', text)
    print("no change; section:", (m.group(0) if m else "missing")[:200])
