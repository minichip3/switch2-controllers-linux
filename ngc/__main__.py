"""Entry point: pair (discover + save) and run (bridge) commands.

    python -m ngc pair     # scan for a controller in pairing mode, save its address
    python -m ngc run      # run the bridge (virtual gamepad + auto-reconnect)
    python -m ngc          # run; if unconfigured, pair first
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time

from . import protocol as P
from .config import Config


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _bond_sync(mac: str, player: int, adapter: str) -> bool:
    """Connect over raw ATT and run the bonding handshake so the controller
    reconnects automatically afterward."""
    from .bridge import prepare_bluez
    from .device import SwitchController

    prepare_bluez(mac, remove=True)
    ctrl = SwitchController(mac, adapter)
    connected = False
    for _ in range(15):
        if ctrl.connect(timeout=6):
            connected = True
            break
    if not connected:
        print("Bonding: could not establish raw link.")
        return False
    try:
        ctrl.initialize(player=player)
        ctrl.bond()
        time.sleep(1.5)
        print(f"Bonded {ctrl.info.name} ({mac}) as player {player} to adapter {adapter}.")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"Bonding failed: {exc}")
        return False
    finally:
        ctrl.close()


async def _pair(
    cfg: Config, timeout: float, player: int | None = None, role: str | None = None
) -> bool:
    from .scanner import find_first

    print(
        "Put a Switch 2 controller (GameCube / Pro Controller 2 / Joy-Con 2) "
        "in pairing mode (hold sync until LEDs sweep)..."
    )
    found = await find_first(timeout=timeout, require_pairing=True)
    if not found:
        print("No controller found in pairing mode.")
        return False
    if role and found.product_id not in (P.JOYCON2_LEFT_PID, P.JOYCON2_RIGHT_PID):
        print(f"--role only applies to a solo Joy-Con 2 half, not {found.name}.")
        return False
    if not cfg.adapter_mac:
        from .config import detect_adapter

        cfg.adapter_mac = detect_adapter()
    if not cfg.adapter_mac:
        print(
            "No Bluetooth adapter found (nothing under /sys/class/bluetooth). "
            "Check that a Bluetooth adapter is present and its driver is loaded."
        )
        return False
    # --role left/right combines this Joy-Con 2 half with its other half into
    # one P<player> pad instead of giving it a player slot of its own: pair
    # the left half with `ngc pair --player 1 --role left`, then the right
    # half with `ngc pair --player 1 --role right`.
    entry = cfg.add_controller(found.device.address, name=found.name, player=player, pair_role=role)
    cfg.save()
    if role:
        print(f"Discovered {found.name} ({role}) at {entry.mac} (player {entry.player}); bonding...")
    else:
        print(f"Discovered {found.name} at {entry.mac} (player {entry.player}); bonding...")
    ok = await asyncio.get_event_loop().run_in_executor(
        None, _bond_sync, entry.mac, entry.player, cfg.adapter_mac
    )
    if ok:
        cfg.mark_bonded(entry.mac, True)
        cfg.save()
    return ok


async def _rebond(cfg: Config, timeout: float) -> bool:
    """Re-run the bonding handshake for a controller already in config."""
    from .scanner import find_first

    print(
        "Hold Sync on the controller until the LEDs sweep (pairing mode), "
        "then release — this re-bonds it for button-wake reconnect."
    )
    found = await find_first(timeout=timeout, require_pairing=True)
    if not found:
        print("No controller found in pairing mode.")
        return False
    macs = {e.mac.upper() for e in cfg.entries()}
    if found.device.address.upper() not in macs:
        print(f"{found.device.address} is not in your saved list — use 'pair' to add it.")
        return False
    if not cfg.adapter_mac:
        from .config import detect_adapter

        cfg.adapter_mac = detect_adapter()
    if not cfg.adapter_mac:
        print(
            "No Bluetooth adapter found (nothing under /sys/class/bluetooth). "
            "Check that a Bluetooth adapter is present and its driver is loaded."
        )
        return False
    entry = cfg.add_controller(found.device.address, name=found.name)
    cfg.save()
    cfg.mark_bonded(found.device.address, False)
    cfg.save()
    print(f"Re-bonding {found.name} at {entry.mac} (player {entry.player})...")
    ok = await asyncio.get_event_loop().run_in_executor(
        None, _bond_sync, entry.mac, entry.player, cfg.adapter_mac
    )
    if ok:
        cfg.mark_bonded(entry.mac, True)
        cfg.save()
    return ok


def _list(cfg: Config) -> int:
    entries = cfg.entries()
    if not entries:
        print("No controllers configured. Run: python -m ngc pair")
        return 0
    print(f"Adapter: {cfg.adapter_mac}")
    for e in entries:
        label = e.name or "Switch 2 Controller"
        bond = "bonded" if e.bonded else "needs bond (connect once with Sync)"
        slot = f"P{e.player} ({e.pair_role})" if e.pair_role else f"P{e.player}"
        print(f"  {slot}  {e.mac}  {label}  [{bond}]")
    return 0


def _remove(cfg: Config, mac: str) -> int:
    # If the MAC is half of a combined pair, free the other half first so it
    # doesn't stay paired to a dead slot (it becomes a standalone controller).
    cfg.uncombine_pair(mac)
    if not cfg.remove_controller(mac):
        print(f"Controller {mac} is not in your saved list.")
        return 1
    cfg.save()
    print(f"Removed {mac.upper()}. Restart the bridge to apply.")
    return 0


def _swap(cfg: Config, player_a: int, player_b: int) -> int:
    ca = cfg.find_by_player(player_a)
    cb = cfg.find_by_player(player_b)
    if not ca or not cb:
        print(f"Could not swap player {player_a} and {player_b} — check both are saved.")
        return 1
    if ca.pair_role or cb.pair_role:
        print("Can't swap a combined Joy-Con 2 pair slot — uncombine it first.")
        return 1
    if not cfg.swap_players(player_a, player_b):
        return 1
    cfg.save()
    la = ca.name or ca.mac
    lb = cb.name or cb.mac
    print(f"Swapped: {la} is now P{player_b}, {lb} is now P{player_a}. Restart the bridge to apply.")
    return 0


def _role(cfg: Config, mac: str, player: int, role: str) -> int:
    """Set an already-saved MAC's player slot / pair role -- config-only,
    like `swap`, no Bluetooth involved (no need to re-pair/re-sync)."""
    mac = mac.upper()
    if not any(e.mac.upper() == mac for e in cfg.entries()):
        print(f"{mac} is not in your saved list — pair it first.")
        return 1
    try:
        entry = cfg.add_controller(mac, player=player, pair_role=role)
    except ValueError as exc:
        print(f"Could not set role: {exc}")
        return 1
    cfg.save()
    print(f"{entry.mac} is now P{entry.player} ({role}). Restart the bridge to apply.")
    return 0


def _infer_role(name: str) -> str | None:
    """Guess which Joy-Con 2 half a saved controller is, from its name."""
    n = (name or "").lower()
    if "left" in n or "(l)" in n:
        return "left"
    if "right" in n or "(r)" in n:
        return "right"
    return None


def _combine(
    cfg: Config,
    player_a: int,
    player_b: int,
    target_player: int | None = None,
) -> int:
    """Combine the Joy-Con 2 halves on two player slots into one pair pad.

    The left/right roles are inferred from each pad's name, so the player
    order doesn't matter (`combine --players 1 2` == `2 1`). Config-only,
    like `swap` -- no Bluetooth involved. The pair takes the lower slot
    (or --target N).
    """
    ca = cfg.find_by_player(player_a)
    cb = cfg.find_by_player(player_b)
    if (ca and ca.pair_role) or (cb and cb.pair_role):
        print(f"Could not combine: P{player_a} or P{player_b} is already half of a pair — uncombine it first.")
        return 1
    if not ca or not cb:
        print(f"Could not combine: need a saved controller on both P{player_a} and P{player_b}.")
        return 1
    role_a, role_b = _infer_role(ca.name), _infer_role(cb.name)
    if not role_a or not role_b:
        unknown = [e.name or e.mac for e, r in ((ca, role_a), (cb, role_b)) if not r]
        print(
            "Could not combine: can't tell which half is left/right for "
            + ", ".join(unknown)
            + ". Set the roles manually: ngc role --mac X --player N --role left"
            " (and --role right for the other half)."
        )
        return 1
    if role_a == role_b:
        print(f"Could not combine: both are {role_a} halves — you need one of each.")
        return 1
    try:
        updated = cfg.combine_players(player_a, player_b, role_a=role_a, target_player=target_player)
    except ValueError as exc:
        print(f"Could not combine: {exc}")
        return 1
    cfg.save()
    pair = next(e for e in updated if e.pair_role)
    names = ", ".join(f"{e.name or e.mac} ({e.pair_role})" for e in updated if e.pair_role)
    print(f"Combined {names} into one P{pair.player} pad. Restart the bridge to apply.")
    return 0


def _uncombine(cfg: Config, mac: str | None = None, player: int | None = None) -> int:
    """Split a combined Joy-Con 2 pair back into two standalone pads."""
    updated = cfg.uncombine_pair(mac) if mac else cfg.uncombine_player(player) if player else None
    if updated is None:
        target = f"{mac.upper()}" if mac else f"P{player}"
        print(f"{target} is not part of a combined pair.")
        return 1
    cfg.save()
    names = ", ".join(f"{e.name or e.mac} (P{e.player})" for e in updated)
    print(f"Uncombined: {names}. Restart the bridge to apply.")
    return 0


def _run(cfg: Config) -> int:
    from .bridge import Bridge

    bridge = Bridge(cfg)

    def _sig(_signum, _frame):
        bridge.stop()

    def _usr1(_signum, _frame):
        # Dolphin: Save to Selected Slot = Back + Shoulder R (C + R on ngc GC pad).
        try:
            bridge.pulse_gamecube_hotkey("C", "R")
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("hotkey pulse failed: %s", exc)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGUSR1, _usr1)
    bridge.run()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ngc", description="Switch 2 controller bridge (GameCube / Pro Controller 2 / Joy-Con 2)")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "pair", "rebond", "list", "remove", "swap", "role", "combine", "uncombine"])
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0, help="pairing scan timeout")
    parser.add_argument("--mac", help="controller MAC (for remove)")
    parser.add_argument("--player", type=int, help="player slot 1-8 (for pair / uncombine)")
    parser.add_argument(
        "--role", choices=["left", "right"],
        help="combine this Joy-Con 2 half with its other half into one player pad (for pair/role)",
    )
    parser.add_argument("--players", nargs=2, type=int, metavar=("A", "B"), help="player slots (for swap / combine / uncombine)")
    parser.add_argument("--target", type=int, metavar="N", help="player slot for the combined pair (combine; default: lower of A/B)")
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    cfg = Config.load()

    if args.command == "pair":
        return 0 if asyncio.run(_pair(cfg, args.timeout, args.player, args.role)) else 1

    if args.command == "rebond":
        return 0 if asyncio.run(_rebond(cfg, args.timeout)) else 1

    if args.command == "remove":
        if not args.mac:
            print("Usage: ngc remove --mac AA:BB:CC:DD:EE:FF")
            return 1
        return _remove(cfg, args.mac)

    if args.command == "swap":
        a, b = (args.players if args.players else (1, 2))
        return _swap(cfg, a, b)

    if args.command == "role":
        if not args.mac or args.player is None or not args.role:
            print("Usage: ngc role --mac AA:BB:CC:DD:EE:FF --player N --role left/right")
            return 1
        return _role(cfg, args.mac, args.player, args.role)

    if args.command == "combine":
        a, b = (args.players if args.players else (1, 2))
        return _combine(cfg, a, b, target_player=args.target)

    if args.command == "uncombine":
        if not args.mac and args.player is None:
            print("Usage: ngc uncombine --mac AA:BB:CC:DD:EE:FF   (or)   ngc uncombine --player 1")
            return 1
        return _uncombine(cfg, args.mac, args.player)

    if args.command == "list":
        return _list(cfg)

    if not cfg.entries():
        print("No controller configured; scanning for one in pairing mode first.")
        if not asyncio.run(_pair(cfg, args.timeout)):
            return 1
    return _run(cfg)


if __name__ == "__main__":
    sys.exit(main())
