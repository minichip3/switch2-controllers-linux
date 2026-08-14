"""Decky Loader backend for Switch 2 Controllers.

Decky's sandbox cannot reach D-Bus, so we set XDG_RUNTIME_DIR explicitly
in each subprocess call via `env=`.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

try:
    import decky
except ImportError:
    decky = None  # type: ignore

PROJECT_DIR = os.environ.get("NGC_PROJECT_DIR", os.path.expanduser("~/nso-gc-bazzite"))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from ngc import control  # noqa: E402


def _log(msg: str) -> None:
    if decky is not None:
        decky.logger.info(msg)


SERVICE = "nso-gc.service"

# ── Environment for systemctl --user inside Decky sandbox ─────────────────


def _ctl_env() -> dict:
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = "/run/user/1000"
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/user/1000/bus"
    return env


def _systemctl_user(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user"] + list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_ctl_env(),
    )


# ── Service helpers ──────────────────────────────────────────────────────


def _service_state() -> str:
    r = _systemctl_user("is-active", SERVICE, timeout=5)
    return (r.stdout or "inactive").strip() or "inactive"


def _ensure_service() -> None:
    _systemctl_user("reset-failed", SERVICE, timeout=5)
    _systemctl_user("enable", "--now", SERVICE, timeout=15)


def _restart_service() -> None:
    _systemctl_user("reset-failed", SERVICE, timeout=5)
    _systemctl_user("restart", SERVICE, timeout=15)


# ── Plugin ───────────────────────────────────────────────────────────────


class Plugin:
    async def _main(self) -> None:
        _log(f"Switch 2 Controllers decky plugin loaded ({PROJECT_DIR})")

    async def _unload(self) -> None:
        _log("Switch 2 Controllers plugin unloaded")

    async def get_status(self) -> dict:
        def work():
            svc = _service_state()
            pads = sorted(control.load_pads(), key=lambda p: p.player)
            state = control.read_bridge_state() if svc == "active" else None
            merged = control.merge_state(pads, state)
            connected = sum(1 for p in merged if p.connected)

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
                names = ", ".join(f"P{p.player}" for p in merged if p.connected)
                headline, detail = f"{connected} connected", f"{names} — ready in Steam"
            else:
                headline, detail = "Ready", "Hold Sync on a saved controller to connect."

            from dataclasses import asdict

            return {
                "service": svc,
                "headline": headline,
                "detail": detail,
                "connected_count": connected,
                "pads": [
                    {
                        **asdict(p),
                        "status": control.pad_status_line(p, svc),
                    }
                    for p in merged
                ],
            }

        return await asyncio.get_event_loop().run_in_executor(None, work)

    async def ensure_bridge(self) -> dict:
        _log("ensure_bridge: calling ensure_service")

        def work():
            _ensure_service()

        await asyncio.get_event_loop().run_in_executor(None, work)
        st = await self.get_status()
        _log(f"ensure_bridge done: service={st.get('service')}")
        return {"ok": True, "status": st}

    async def add_controller(self) -> dict:
        def work() -> tuple[int, str]:
            rc, out = control.run_ngc(["pair", "--timeout", "60"], timeout=150, stop_service=True)
            return rc, out

        rc, out = await asyncio.get_event_loop().run_in_executor(None, work)
        return {"ok": rc == 0, "message": out[-1500:] if out else ""}

    async def remove_controller(self, mac: str) -> dict:
        def work() -> tuple[int, str]:
            return control.run_config(["remove", "--mac", mac.upper()])

        rc, out = await asyncio.get_event_loop().run_in_executor(None, work)
        return {"ok": rc == 0, "message": out[-1500:] if out else ""}

    async def repair_controller(self, mac: str, player: int) -> dict:
        mac = mac.upper()

        def work() -> tuple[int, str]:
            rc, out = control.run_config(["remove", "--mac", mac], restart=False)
            if rc != 0:
                return rc, out
            return control.run_ngc(
                ["pair", "--timeout", "60", "--player", str(int(player))],
                timeout=150,
                stop_service=True,
            )

        rc, out = await asyncio.get_event_loop().run_in_executor(None, work)
        return {"ok": rc == 0, "message": out[-1500:] if out else ""}

    async def swap_players(self) -> dict:
        def work() -> tuple[int, str]:
            return control.run_config(["swap", "--players", "1", "2"])

        rc, out = await asyncio.get_event_loop().run_in_executor(None, work)
        return {"ok": rc == 0, "message": out[-1500:] if out else ""}

    async def rebond(self) -> dict:
        def work() -> tuple[int, str]:
            return control.run_ngc(["rebond", "--timeout", "45"], timeout=120, stop_service=True)

        rc, out = await asyncio.get_event_loop().run_in_executor(None, work)
        return {"ok": rc == 0, "message": out[-1500:] if out else ""}

    async def restart_bridge(self) -> dict:
        def work() -> None:
            _restart_service()

        await asyncio.get_event_loop().run_in_executor(None, work)
        return {"ok": True, "status": await self.get_status()}

    async def get_logs(self) -> str:
        def work() -> str:
            r = _systemctl_user("status", SERVICE, timeout=10)
            return (r.stdout or r.stderr or "(empty)").strip()[-3000:]

        return await asyncio.get_event_loop().run_in_executor(None, work)
