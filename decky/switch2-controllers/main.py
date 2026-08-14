"""Decky Loader backend for Switch 2 Controllers.

Decky's PyInstaller bundle leaks LD_LIBRARY_PATH into every subprocess,
which breaks systemctl (wrong libcrypto.so.3). Clear it at import time.
Also set XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS for systemctl --user.
"""

from __future__ import annotations

import asyncio
import os
import sys

# ── Critical fix: clear LD_LIBRARY_PATH leaked by Decky PyInstaller ───────
os.environ.pop("LD_LIBRARY_PATH", None)

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


# ── Decky env for systemctl --user ──────────────────────────────────────

def _decky_env() -> dict:
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = "/run/user/1000"
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/user/1000/bus"
    return env


# Patch control._run so systemctl calls get the Decky env
_original_run = control._run


def _decky_run(cmd, *, timeout=30.0, cwd=None, env=None):
    if env is None:
        env = _decky_env()
    return _original_run(cmd, timeout=timeout, cwd=cwd, env=env)


control._run = _decky_run


# ── Plugin ───────────────────────────────────────────────────────────────


class Plugin:
    async def _main(self) -> None:
        _log(f"Switch 2 Controllers decky plugin loaded ({PROJECT_DIR})")

    async def _unload(self) -> None:
        _log("Switch 2 Controllers plugin unloaded")

    async def get_status(self) -> dict:
        return await asyncio.get_event_loop().run_in_executor(None, control.get_status)

    async def ensure_bridge(self) -> dict:
        _log("ensure_bridge: calling ensure_service")

        def work():
            control.ensure_service()

        await asyncio.get_event_loop().run_in_executor(None, work)
        st = await self.get_status()
        _log(f"ensure_bridge done: service={st.get('service')}")
        return {"ok": True, "status": st}

    async def add_controller(self) -> dict:
        def work() -> tuple[int, str]:
            return control.run_ngc(["pair", "--timeout", "60"], timeout=150, stop_service=True)

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
            control.restart_service()

        await asyncio.get_event_loop().run_in_executor(None, work)
        return {"ok": True, "status": await self.get_status()}

    async def get_logs(self) -> str:
        return await asyncio.get_event_loop().run_in_executor(None, control.recent_logs)
