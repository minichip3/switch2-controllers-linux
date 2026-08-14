"""Shared control-plane helpers for GTK GUI and Decky plugin."""

from __future__ import annotations

import getpass
import json
import os
import pwd
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

PROJECT_DIR = Path(os.environ.get("NGC_PROJECT_DIR", Path.home() / "nso-gc-bazzite"))
PY = Path(os.environ.get("NGC_PYTHON", PROJECT_DIR / ".venv312" / "bin" / "python"))
SERVICE = "nso-gc.service"
STATE_PATH = Path.home() / ".config" / "nso-gc" / "state.json"
STATE_STALE_S = 8.0

# Error strings that indicate `systemctl --user` / `journalctl --user` couldn't
# reach the D-Bus session bus at all (as opposed to some other failure like
# the unit not existing). Used to decide whether the --machine=<user>@
# fallback is worth trying.
_BUS_ERROR_MARKERS = (
    "failed to connect to bus",
    "failed to connect to user scope bus",
    "connection refused",
    "no medium found",
)


@dataclass
class PadStatus:
    player: int
    name: str
    mac: str
    bonded: bool
    connected: bool = False
    battery_pct: Optional[int] = None


def _run(cmd: list[str], *, timeout: float = 30.0, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd or str(PROJECT_DIR))


def _target_username() -> str:
    """User whose systemd --user instance / D-Bus session we want to reach.

    Decky sets DECKY_USER to the unprivileged user it dropped privileges to
    (see SandboxedPlugin.initialize in decky-loader); fall back to whoever
    this process is actually running as when invoked outside Decky (GUI, CLI).
    Never hardcode a username.
    """
    return os.environ.get("DECKY_USER") or getpass.getuser()


def _target_uid(username: str) -> Optional[int]:
    try:
        return pwd.getpwnam(username).pw_uid
    except KeyError:
        return None


def _user_ctl_env(username: str) -> dict[str, str]:
    """Environment for talking to `username`'s systemd --user / D-Bus session.

    Decky Loader (as of v3.x, see SandboxedPlugin.initialize) sets HOME,
    USER and DECKY_USER for the plugin subprocess but never XDG_RUNTIME_DIR
    or DBUS_SESSION_BUS_ADDRESS, so `systemctl --user` can't find the
    session bus socket at /run/user/<uid>/bus even though the plugin really
    is running as the unprivileged user. Fill both in explicitly so the env
    reaches subprocess.run() regardless of what main.py did to os.environ
    (each subprocess.run() call gets its own env, it isn't inherited from a
    one-time os.environ mutation made earlier in the process).
    """
    env = dict(os.environ)
    uid = _target_uid(username)
    if uid is not None:
        runtime_dir = f"/run/user/{uid}"
        if os.path.isdir(runtime_dir):
            env["XDG_RUNTIME_DIR"] = runtime_dir
            env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
    return env


def _looks_like_bus_error(stderr: str) -> bool:
    low = (stderr or "").lower()
    return any(marker in low for marker in _BUS_ERROR_MARKERS)


def _run_user_ctl(binary: str, args: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    """Run `<binary> --user <args>` targeting the current/Decky user's session.

    Two layers of defense against the missing-XDG_RUNTIME_DIR problem:
    1. Pass XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS explicitly via `env=`
       so it doesn't depend on os.environ mutations happening to propagate.
    2. If that still can't reach the bus (e.g. /run/user/<uid> doesn't
       exist yet), fall back to `--machine=<user>@`, which asks systemd to
       look up the user's --user manager instance via machined instead of
       relying on our own XDG_RUNTIME_DIR at all.
    """
    username = _target_username()
    cmd = [binary, "--user", *args]
    r = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=str(PROJECT_DIR), env=_user_ctl_env(username)
    )
    if r.returncode != 0 and _looks_like_bus_error(r.stderr):
        cmd_fallback = [binary, "--user", f"--machine={username}@", *args]
        r_fallback = subprocess.run(
            cmd_fallback, capture_output=True, text=True, timeout=timeout, cwd=str(PROJECT_DIR)
        )
        if r_fallback.returncode == 0 or not _looks_like_bus_error(r_fallback.stderr):
            return r_fallback
    return r


def service_state() -> str:
    r = _run_user_ctl("systemctl", ["is-active", SERVICE], timeout=5)
    return (r.stdout or "inactive").strip() or "inactive"


def ensure_service() -> None:
    _run_user_ctl("systemctl", ["reset-failed", SERVICE], timeout=5)
    _run_user_ctl("systemctl", ["enable", "--now", SERVICE], timeout=15)


def restart_service() -> None:
    _run_user_ctl("systemctl", ["reset-failed", SERVICE], timeout=5)
    _run_user_ctl("systemctl", ["restart", SERVICE], timeout=15)


def load_pads() -> list[PadStatus]:
    if PY.is_file():
        r = _run([str(PY), "-m", "ngc", "list"], timeout=10)
        pads: list[PadStatus] = []
        row_re = re.compile(
            r"P(\d+)\s+([0-9A-F:]{17})\s+(.+?)\s+\[(.+)\]\s*$", re.IGNORECASE
        )
        for line in (r.stdout or "").splitlines():
            m = row_re.match(line.strip())
            if not m:
                continue
            flags = m.group(4).lower()
            pads.append(
                PadStatus(
                    int(m.group(1)),
                    m.group(3).strip(),
                    m.group(2).upper(),
                    "bonded" in flags and "needs bond" not in flags,
                )
            )
        if pads:
            return pads
    cfg_path = Path.home() / ".config" / "nso-gc" / "config.json"
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text())
            return [
                PadStatus(
                    c.get("player", i + 1),
                    c.get("name") or "Switch 2 Controller",
                    c["mac"].upper(),
                    bool(c.get("bonded", False)),
                )
                for i, c in enumerate(data.get("controllers") or [])
            ]
        except Exception:
            pass
    return []


def read_bridge_state() -> Optional[dict]:
    if not STATE_PATH.is_file():
        return None
    try:
        data = json.loads(STATE_PATH.read_text())
        updated = float(data.get("updated_at") or 0)
        if updated and time.time() - updated > STATE_STALE_S:
            return None
        return data
    except Exception:
        return None


def merge_state(pads: list[PadStatus], state: Optional[dict]) -> list[PadStatus]:
    if not state:
        return pads
    by_mac = {c.get("mac", "").upper(): c for c in state.get("controllers") or []}
    out: list[PadStatus] = []
    for pad in pads:
        live = by_mac.get(pad.mac, {})
        out.append(
            PadStatus(
                pad.player,
                live.get("name") or pad.name,
                pad.mac,
                pad.bonded,
                connected=bool(live.get("connected")),
                battery_pct=live.get("battery_pct"),
            )
        )
    return out


def pad_status_line(pad: PadStatus, service: str) -> str:
    if service != "active":
        return "Bridge stopped"
    if pad.connected:
        pct = f" · {pad.battery_pct}%" if pad.battery_pct is not None else ""
        return f"Connected{pct}"
    if not pad.bonded:
        return "Needs setup"
    return "Hold Sync to connect"


def get_status() -> dict[str, Any]:
    svc = service_state()
    pads = sorted(merge_state(load_pads(), read_bridge_state() if svc == "active" else None), key=lambda p: p.player)
    connected = sum(1 for p in pads if p.connected)
    state = read_bridge_state() if svc == "active" else None

    if svc != "active":
        headline, detail = "Bridge is off", "Start the bridge to connect controllers."
    elif state and state.get("hub_error"):
        headline, detail = "Needs attention", str(state.get("hub_error", ""))[:200]
    elif state:
        headline = str(state.get("headline") or "Ready")
        detail = str(state.get("detail") or "Hold Sync on a saved controller to connect.")
    elif not pads:
        headline, detail = "Get started", "Add your first controller with Sync."
    elif connected:
        names = ", ".join(f"P{p.player}" for p in pads if p.connected)
        headline, detail = f"{connected} connected", f"{names} — ready in Steam"
    else:
        headline, detail = "Ready", "Hold Sync on a saved controller to connect."

    return {
        "service": svc,
        "headline": headline,
        "detail": detail,
        "connected_count": connected,
        "pads": [
            {
                **asdict(p),
                "status": pad_status_line(p, svc),
            }
            for p in pads
        ],
    }


def run_ngc(args: list[str], *, timeout: float = 120.0, stop_service: bool = False) -> tuple[int, str]:
    if stop_service:
        _run_user_ctl("systemctl", ["stop", SERVICE], timeout=10)
    r = _run([str(PY), "-m", "ngc", *args], timeout=timeout)
    ensure_service()
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def run_config(args: list[str], *, restart: bool = True) -> tuple[int, str]:
    r = _run([str(PY), "-m", "ngc", *args], timeout=30)
    if restart:
        restart_service()
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def recent_logs(lines: int = 35) -> str:
    r = _run_user_ctl(
        "journalctl", ["-u", SERVICE, "-n", str(lines), "--no-pager", "-o", "cat"], timeout=10
    )
    return (r.stdout or r.stderr or "(empty)").strip()[-3000:]
