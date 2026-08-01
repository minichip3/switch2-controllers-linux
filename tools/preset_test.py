#!/usr/bin/env python3
"""Sweep GameCube vibration presets (1=gentle, 2=strong, 3=light tap).

Usage:
  python tools/preset_test.py                    # GameCube from config, auto adapter
  python tools/preset_test.py 1,2,3              # preset list only
  python tools/preset_test.py MAC ADAPTER 1,2,3  # explicit MACs

Hold Sync on the GameCube pad while connecting. Stop nso-gc.service first:
  systemctl --user stop nso-gc.service
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ngc.config import Config, detect_adapter
from ngc import protocol as P
from ngc.device import SwitchController

logging.basicConfig(level=logging.WARNING)


def parse_presets(spec: str) -> list[int]:
    if "," in spec:
        return [int(x.strip()) for x in spec.split(",") if x.strip()]
    n = int(spec)
    return list(range(1, n + 1))


def pick_gamecube_mac(cfg: Config) -> str:
    for entry in cfg.entries():
        if "gamecube" in (entry.name or "").lower():
            return entry.mac.upper()
        if entry.mac and not P.has_hd_rumble(P.NSO_GAMECUBE_PID):
            pass
    for entry in cfg.entries():
        name = (entry.name or "").lower()
        if "pro" not in name and "joy" not in name:
            return entry.mac.upper()
    gc = [e for e in cfg.entries() if "gamecube" in (e.name or "").lower()]
    if gc:
        return gc[0].mac.upper()
    entries = cfg.entries()
    if len(entries) == 1:
        return entries[0].mac.upper()
    raise SystemExit(
        "Could not pick a GameCube pad from config. Pass the pad MAC:\n"
        "  python tools/preset_test.py 3C:A9:AB:5E:6D:37"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Test NSO GameCube vibration presets")
    parser.add_argument(
        "mac",
        nargs="?",
        help="Controller MAC (default: GameCube from ~/.config/nso-gc/config.json)",
    )
    parser.add_argument(
        "adapter_or_presets",
        nargs="?",
        help="Adapter MAC, or preset list if MAC omitted",
    )
    parser.add_argument(
        "presets",
        nargs="?",
        default="1,2,3",
        help="Preset IDs or list, e.g. 1,2,3 (default: 1,2,3)",
    )
    args = parser.parse_args()

    cfg = Config.load()
    mac = args.mac
    adapter = cfg.adapter_mac or detect_adapter()
    preset_spec = args.presets

    if mac is None:
        mac = pick_gamecube_mac(cfg)
        if args.adapter_or_presets and ":" in args.adapter_or_presets.upper():
            adapter = args.adapter_or_presets.upper()
            preset_spec = args.presets
        elif args.adapter_or_presets:
            preset_spec = args.adapter_or_presets
    elif args.adapter_or_presets:
        if ":" in args.adapter_or_presets.upper():
            adapter = args.adapter_or_presets.upper()
        else:
            preset_spec = args.adapter_or_presets

    if not adapter:
        raise SystemExit("No Bluetooth adapter found — pass adapter MAC as 2nd argument.")

    presets = parse_presets(preset_spec)
    mac = mac.upper()
    adapter = adapter.upper()

    print(f"Controller: {mac}")
    print(f"Adapter:    {adapter}")
    print(f"Presets:    {presets}")
    print("Hold Sync on the GameCube pad now…\n", flush=True)

    ctrl = SwitchController(mac, adapter)
    deadline = time.time() + 45
    while time.time() < deadline:
        if ctrl.connect(timeout=8):
            break
    else:
        print("Could not connect — hold Sync and try again.")
        return 1

    ctrl.enable_commands()
    print(f"Connected. Testing presets {presets} (LEDs show preset number mod 4).\n", flush=True)
    for n in presets:
        led = ((n - 1) % 4) + 1
        ctrl.set_player_leds(led)
        label = {1: "medium", 2: "strongest", 3: "softest"}.get(n, "")
        extra = f" ({label})" if label else ""
        print(f">>> PRESET {n}{extra}  (LEDs: {led})", flush=True)
        try:
            ctrl.play_vibration_preset(n)
        except Exception as exc:  # noqa: BLE001
            print(f"    preset {n} error: {exc}")
        time.sleep(3.0)
    ctrl.set_player_leds(1)
    print("\nDone.", flush=True)
    ctrl.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
