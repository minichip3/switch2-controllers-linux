"""Persistent configuration for the bridge.

Supports multiple bonded controllers (for local multiplayer): each entry has a
MAC, an assigned player slot, and a remembered name. Stored as JSON under XDG
config. Legacy single-controller configs are migrated automatically on load.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "nso-gc"
CONFIG_PATH = CONFIG_DIR / "config.json"
CONFIG_BACKUP = CONFIG_DIR / "config.json.bak"


def _detect_adapter_dbus() -> Optional[str]:
    """Ask BlueZ over D-Bus for an adapter's address.

    Fallback for when sysfs hasn't populated hciN/address yet -- real-world
    finding: a fresh Ubuntu live-USB boot had BlueZ fully Powered with a
    working adapter (GUI pairing of another device succeeded) while
    /sys/class/bluetooth/hci0/address never appeared, so the sysfs-only
    check below returned None even though a usable adapter existed.
    """
    for idx in range(4):
        try:
            out = subprocess.run(
                ["busctl", "--system", "get-property", "org.bluez", f"/org/bluez/hci{idx}",
                 "org.bluez.Adapter1", "Address"],
                capture_output=True, text=True, timeout=2.0,
            )
        except Exception:  # noqa: BLE001
            continue
        if out.returncode != 0:
            continue
        parts = out.stdout.strip().split(None, 1)
        if len(parts) == 2:
            return parts[1].strip('"').upper()
    return None


def detect_adapter() -> Optional[str]:
    """Return the first Bluetooth adapter's address.

    Prefers sysfs (no root, no subprocess), falling back to D-Bus if that
    finds nothing -- see _detect_adapter_dbus.
    """
    base = Path("/sys/class/bluetooth")
    if base.exists():
        for hci in sorted(base.iterdir()):
            addr = hci / "address"
            if addr.exists():
                try:
                    return addr.read_text().strip().upper()
                except OSError:
                    continue
    return _detect_adapter_dbus()


@dataclass
class ControllerEntry:
    mac: str
    player: int = 1
    name: str = ""
    bonded: bool = False
    # "left"/"right" when this MAC is one half of a Joy-Con 2 pair that shares
    # its player slot with its other half (see bridge._PairGroup); None for a
    # standalone controller (Pro / GameCube / solo Joy-Con).
    pair_role: Optional[str] = None
    # LE address type (att.LE_PUBLIC=1 / att.LE_RANDOM=2) that last actually
    # connected for this MAC, persisted across process restarts so a fresh
    # connect tries the known-good type first instead of a blind 50/50 that
    # can burn the pad's advertising window on the wrong guess (see
    # bridge._Worker.last_dst_type). None until a connect has ever succeeded.
    dst_type: Optional[int] = None


@dataclass
class Config:
    controllers: list = field(default_factory=list)  # list of {mac, player, name}
    adapter_mac: Optional[str] = None
    button_map: dict = field(default_factory=dict)
    # Rumble: GameCube uses safe presets; Pro/Joy-Con use the real HD motor.
    enable_rumble: bool = True
    # Legacy single-controller fields (migrated into `controllers` on load).
    controller_mac: Optional[str] = None
    player: int = 1

    @classmethod
    def _read_json(cls, path: Path) -> dict:
        return json.loads(path.read_text())

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            try:
                data = cls._read_json(CONFIG_PATH)
            except Exception:
                if CONFIG_BACKUP.exists():
                    try:
                        data = cls._read_json(CONFIG_BACKUP)
                    except Exception:
                        cfg = cls()
                    else:
                        cfg = cls(**{k: data[k] for k in data if k in cls.__dataclass_fields__})
                else:
                    cfg = cls()
            else:
                cfg = cls(**{k: data[k] for k in data if k in cls.__dataclass_fields__})
        else:
            cfg = cls()
        cfg._migrate()
        if not cfg.adapter_mac:
            cfg.adapter_mac = detect_adapter()
        return cfg

    def _migrate(self) -> None:
        """Fold a legacy single controller into the controllers list."""
        if not self.controllers and self.controller_mac:
            self.controllers = [
                {"mac": self.controller_mac, "player": self.player, "name": ""}
            ]
            self.controller_mac = None

    def entries(self) -> list[ControllerEntry]:
        return [
            ControllerEntry(
                mac=c["mac"],
                player=c.get("player", i + 1),
                name=c.get("name", ""),
                bonded=bool(c.get("bonded", False)),
                pair_role=c.get("pair_role"),
                dst_type=c.get("dst_type"),
            )
            for i, c in enumerate(self.controllers)
        ]

    def mark_bonded(self, mac: str, bonded: bool = True) -> None:
        for c in self.controllers:
            if c["mac"].upper() == mac.upper():
                c["bonded"] = bonded
                return

    def mark_dst_type(self, mac: str, dst_type: int) -> bool:
        """Persist the LE address type that just connected for `mac`.

        Returns True if this changed the stored value (caller should save()),
        False if it already matched (skip the disk write on the common case
        of every connect re-confirming the same type).
        """
        for c in self.controllers:
            if c["mac"].upper() == mac.upper():
                if c.get("dst_type") == dst_type:
                    return False
                c["dst_type"] = dst_type
                return True
        return False

    def is_bonded(self, mac: str) -> bool:
        for c in self.controllers:
            if c["mac"].upper() == mac.upper():
                return bool(c.get("bonded", False))
        return False

    def _player_taken(self, player: int, mac: str, pair_role: Optional[str]) -> bool:
        """True if `player` can't be assigned to `mac`/`pair_role`.

        Two MACs may share a player slot only as complementary Joy-Con 2
        halves (one "left", one "right") -- anything else (same role twice,
        or either side unset) is a real conflict.
        """
        for c in self.controllers:
            if c["mac"].upper() == mac or c.get("player") != player:
                continue
            other_role = c.get("pair_role")
            if pair_role and other_role and pair_role != other_role:
                continue
            return True
        return False

    def add_controller(
        self, mac: str, name: str = "", player: int | None = None, pair_role: str | None = None
    ) -> ControllerEntry:
        """Add (or update) a controller, assigning the next free player slot.

        `pair_role` ("left"/"right") lets this MAC share a player slot with
        its other Joy-Con 2 half instead of taking a slot of its own.
        """
        mac = mac.upper()
        if pair_role not in (None, "left", "right"):
            raise ValueError("pair_role must be 'left', 'right', or None")
        for c in self.controllers:
            if c["mac"].upper() == mac:
                if name:
                    c["name"] = name
                if pair_role is not None:
                    c["pair_role"] = pair_role
                if player is not None:
                    if self._player_taken(player, mac, pair_role if pair_role is not None else c.get("pair_role")):
                        raise ValueError(f"player {player} already in use")
                    c["player"] = player
                return ControllerEntry(
                    c["mac"], c.get("player", 1), c.get("name", ""), bool(c.get("bonded", False)),
                    c.get("pair_role"),
                )
        if player is not None:
            if self._player_taken(player, mac, pair_role):
                raise ValueError(f"player {player} already in use")
            assigned = player
        else:
            if pair_role:
                raise ValueError("pair_role requires an explicit --player slot")
            used = {c.get("player", 0) for c in self.controllers}
            assigned = next((p for p in range(1, 9) if p not in used), None)
            if assigned is None:
                raise ValueError("maximum 8 controllers already saved")
        entry = {"mac": mac, "player": assigned, "name": name, "bonded": False, "pair_role": pair_role}
        self.controllers.append(entry)
        return ControllerEntry(mac, assigned, name, False, pair_role)

    def remove_controller(self, mac: str) -> bool:
        mac = mac.upper()
        before = len(self.controllers)
        self.controllers = [c for c in self.controllers if c["mac"].upper() != mac]
        return len(self.controllers) < before

    def _next_free_player(self, exclude: set[int] | None = None) -> int | None:
        used = {c.get("player", 0) for c in self.controllers}
        if exclude:
            used -= set(exclude)
        return next((p for p in range(1, 9) if p not in used), None)

    def uncombine_pair(self, mac: str) -> Optional[list[ControllerEntry]]:
        """Split a combined Joy-Con 2 pair back into two standalone pads.

        The half named by `mac` keeps its player slot; the other half moves to
        the next free slot (two halves shared one player number, and two
        standalone controllers can't). Returns the updated entries, or None
        if `mac` isn't in a pair.
        """
        mac = mac.upper()
        entries = self.entries()
        target = next((e for e in entries if e.mac.upper() == mac), None)
        if not target or not target.pair_role:
            return None
        mate = next(
            (e for e in entries
             if e.mac.upper() != mac and e.player == target.player and e.pair_role),
            None,
        )
        for c in self.controllers:
            if c["mac"].upper() == mac:
                c["pair_role"] = None
            elif mate and c["mac"].upper() == mate.mac.upper():
                c["pair_role"] = None
                # The target keeps its slot; the mate needs a free one. `used`
                # already includes the target's player, so no exclude needed.
                c["player"] = self._next_free_player() or target.player
        return self.entries()

    def uncombine_player(self, player: int) -> Optional[list[ControllerEntry]]:
        """Split the pair on `player`'s slot (both halves share the number)."""
        entry = self.find_by_player(player)
        if not entry:
            return None
        return self.uncombine_pair(entry.mac)

    def combine_players(
        self,
        player_a: int,
        player_b: int,
        role_a: str,
        target_player: Optional[int] = None,
    ) -> list[ControllerEntry]:
        """Combine the pads on two player slots into one Joy-Con 2 pair.

        `role_a` ("left"/"right") is the role of `player_a`'s pad; `player_b`'s
        pad gets the opposite role. The pair takes the lower of the two slots
        (or `target_player`). Raises ValueError for bad/ambiguous inputs;
        returns all entries on success.
        """
        if role_a not in ("left", "right"):
            raise ValueError("role_a must be 'left' or 'right'")
        if player_a == player_b:
            raise ValueError("players A and B must be different slots")
        ca = self.find_by_player(player_a)
        cb = self.find_by_player(player_b)
        if ca and ca.pair_role:
            raise ValueError(f"P{player_a} is already half of a pair — uncombine it first")
        if cb and cb.pair_role:
            raise ValueError(f"P{player_b} is already half of a pair — uncombine it first")
        if not ca or not cb:
            raise ValueError(f"need a saved controller on both P{player_a} and P{player_b}")
        role_b = "right" if role_a == "left" else "left"
        target = target_player or min(player_a, player_b)
        if target not in (player_a, player_b) and self._player_taken(target, ca.mac, role_a):
            raise ValueError(f"player {target} already in use")
        # Update in an order that never trips the shared-slot check: the side
        # already on the target slot first, then the other side moves in.
        first, second = (ca, cb) if target == player_a else (cb, ca)
        first_role, second_role = (
            (role_a, role_b) if first.mac == ca.mac else (role_b, role_a)
        )
        self.add_controller(first.mac, player=target, pair_role=first_role)
        self.add_controller(second.mac, player=target, pair_role=second_role)
        return self.entries()

    def swap_players(self, player_a: int, player_b: int) -> bool:
        ca = cb = None
        for c in self.controllers:
            if c.get("player") == player_a:
                ca = c
            elif c.get("player") == player_b:
                cb = c
        if not ca or not cb:
            return False
        ca["player"], cb["player"] = player_b, player_a
        return True

    def find_by_player(self, player: int) -> Optional[ControllerEntry]:
        for c in self.controllers:
            if c.get("player") == player:
                return ControllerEntry(
                    c["mac"], c.get("player", 1), c.get("name", ""), bool(c.get("bonded", False)),
                    c.get("pair_role"),
                )
        return None

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Drop legacy fields from the serialized form once migrated.
        data = asdict(self)
        if self.controllers:
            data.pop("controller_mac", None)
        self.save_path(data)

    def save_path(self, data: dict) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(payload)
        os.replace(tmp, CONFIG_PATH)
        try:
            shutil.copy2(CONFIG_PATH, CONFIG_BACKUP)
        except OSError:
            pass
