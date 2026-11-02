"""Applying changes to this app itself: rebuild the UI and restart the server from the browser."""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid

from . import RESTART_EXIT_CODE
from .bus import Bus
from .config import PROJECT_ROOT

BOOT_ID = uuid.uuid4().hex
FRONTEND = PROJECT_ROOT / "frontend"
SERVER_SOURCES = (PROJECT_ROOT / "src",)
UI_SOURCES = (FRONTEND / "src", FRONTEND / "index.html", FRONTEND / "vite.config.ts", FRONTEND / "public")


class AdminError(Exception):
    pass


def classify(path) -> str | None:
    """Which part of this app a changed file belongs to: 'server', 'ui', or None."""
    if any(path == p or p in path.parents for p in SERVER_SOURCES):
        return "server" if path.suffix == ".py" else None
    if any(path == p or p in path.parents for p in UI_SOURCES):
        return "ui"
    return None


class Admin:
    def __init__(self, bus: Bus):
        self.bus = bus
        self.pending = {"server": False, "ui": False}
        self.building = False

    @property
    def supervised(self) -> bool:
        return os.environ.get("PA_SUPERVISED") == "1"

    def status(self) -> dict:
        return {"boot_id": BOOT_ID, "pending": self.pending, "building": self.building, "supervised": self.supervised}

    async def mark_changed(self, part: str) -> None:
        if not self.pending[part]:
            self.pending[part] = True
            await self.bus.publish({"type": "admin_status", **self.status()})

    async def rebuild_ui(self) -> None:
        if self.building:
            raise AdminError("a build is already running")
        npm = shutil.which("npm")
        if not npm:
            raise AdminError("npm not found on PATH")
        self.building = True
        await self.bus.publish({"type": "admin_status", **self.status()})
        ok = False
        try:
            proc = await asyncio.create_subprocess_exec(
                npm, "run", "build", cwd=FRONTEND,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            assert proc.stdout
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    await self.bus.publish({"type": "admin_log", "line": line[:400]})
            ok = await proc.wait() == 0
            if ok:
                self.pending["ui"] = False
        finally:
            self.building = False
            await self.bus.publish({"type": "admin_build_done", "ok": ok, **self.status()})

    async def restart(self) -> None:
        if not self.supervised:
            raise AdminError("restart needs the server started with `uv run personal-agent`")
        await self.bus.publish({"type": "admin_restarting"})
        # Exit after the response goes out; the supervisor starts a fresh server process.
        asyncio.get_running_loop().call_later(0.5, os._exit, RESTART_EXIT_CODE)
