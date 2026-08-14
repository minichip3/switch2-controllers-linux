"""Decky Loader backend for Switch 2 Controllers.

Decky's sandbox cannot reach D-Bus for systemctl --user, so we manage
the ngc bridge process directly via subprocess.Popen.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

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

_PY = Path(PROJECT_DIR) / ".venv312" / "bin" / "python"

# ── Direct bridge process ────────────────────────────────────────────────

_bridge_proc: subprocess.Popen | None = None


def _is_running() -> bool:
    if _bridge_proc is None:
        return False
    return _bridge_proc.poll() is None


def _start() -> bool:
    global _bridge_proc
    if _is_running():
        return True
    if not _PY.is_file():
        _log(f"Python not found: {_PY}")
        return False
    env = dict(os.environ)
    env["HOME"] = str(Path.home())
    _bridge_proc = subprocess.Popen(
        [str(_PY), "-m", "ngc", "run"],
        cwd=str(Path(PROJECT_DIR)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    _log(f"Started ngc bridge (pid={_bridge_proc.pid})")
    return True


def _stop() -> None:
    global _bridge_proc
    if _bridge_proc is None:
        return
    try:
        os.killpg(os.getpgid(_bridge_proc.pid), signal.SIGTERM)
        _bridge_proc.wait(timeout=5)
    except Exception as e:
        _log(f"Stop error: {e}")
        try:
            os.killpg(os.getpgid(_bridge_proc.pid), signal.SIGKILL)
        except Exception:
            pass
    _bridge_proc = None
    _log("Stopped ngc bridge")


def _cli(args: list[str], *, timeout: float = 120.0) -> tuple[int, str]:
    r = subprocess.run(
        [str(_PY), "-m", "ngc"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(Path(PROJECT_DIR)),
    )
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def _get_status() -> dict:
    svc = "active" if _is_running() else "inactive"
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


# ── Plugin ───────────────────────────────────────────────────────────────


class Plugin:
    async def _main(self) -> None:
        _log(f"Switch 2 Controllers decky plugin loaded ({PROJECT_DIR})")

    async def _unload(self) -> None:
        _log("Unloading Switch 2 Controllers")
        _stop()

    async def get_status(self) -> dict:
        return await asyncio.get_event_loop().run_in_executor(None, _get_status)

    async def ensure_bridge(self) -> dict:
        _log("ensure_bridge: starting bridge")

        def work():
            _start()

        await asyncio.get_event_loop().run_in_executor(None, work)
        st = await self.get_status()
        _log(f"ensure_bridge done: service={st.get('service')}")
        return {"ok": True, "status": st}

    async def add_controller(self) -> dict:
        def work():
            _stop()
            rc, out = _cli(["pair", "--timeout", "60"], timeout=150)
            _start()
            return rc, out

        rc, out = await asyncio.get_event_loop().run_in_executor(None, work)
        return {"ok": rc == 0, "message": out[-1500:] if out else ""}

    async def remove_controller(self, mac: str) -> dict:
        def work():
            rc, out = _cli(["remove", "--mac", mac.upper()])
            _stop()
            _start()
            return rc, out

        rc, out = await asyncio.get_event_loop().run_in_executor(None, work)
        return {"ok": rc == 0, "message": out[-1500:] if out else ""}

    async def repair_controller(self, mac: str, player: int) -> dict:
        mac = mac.upper()

        def work():
            rc, out = _cli(["remove", "--mac", mac])
            if rc != 0:
                return rc, out
            _stop()
            rc, out = _cli(["pair", "--timeout", "60", "--player", str(int(player))], timeout=150)
            _start()
            return rc, out

        rc, out = await asyncio.get_event_loop().run_in_executor(None, work)
        return {"ok": rc == 0, "message": out[-1500:] if out else ""}

    async def swap_players(self) -> dict:
        def work():
            rc, out = _cli(["swap", "--players", "1", "2"])
            _stop()
            _start()
            return rc, out

        rc, out = await asyncio.get_event_loop().run_in_executor(None, work)
        return {"ok": rc == 0, "message": out[-1500:] if out else ""}

    async def rebond(self) -> dict:
        def work():
            _stop()
            rc, out = _cli(["rebond", "--timeout", "45"], timeout=120)
            _start()
            return rc, out

        rc, out = await asyncio.get_event_loop().run_in_executor(None, work)
        return {"ok": rc == 0, "message": out[-1500:] if out else ""}

    async def restart_bridge(self) -> dict:
        def work():
            _stop()
            _start()

        await asyncio.get_event_loop().run_in_executor(None, work)
        return {"ok": True, "status": await self.get_status()}

    async def get_logs(self) -> str:
        def work():
            state = control.read_bridge_state()
            if state:
                import json

                return json.dumps(state, indent=2)
            return "(no bridge logs available)"

        return await asyncio.get_event_loop().run_in_executor(None, work)
