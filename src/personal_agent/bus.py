"""Fan-out of server events to every connected browser, plus pending approval requests."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import WebSocket

from .notify import Notifier

APPROVAL_TIMEOUT_S = 600


class Bus:
    def __init__(self, notifier: Notifier | None = None) -> None:
        self.notifier = notifier
        self.clients: set[WebSocket] = set()
        self.pending: dict[str, tuple[asyncio.Future[bool], dict[str, Any]]] = {}
        self.rate_limits: dict[str, Any] = {}

    async def publish(self, event: dict[str, Any]) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_json(event)
            except Exception:
                self.clients.discard(ws)

    async def connect(self, ws: WebSocket) -> None:
        self.clients.add(ws)
        if self.rate_limits:
            await ws.send_json({"type": "rate_limit", "windows": self.rate_limits})
        for _, request in self.pending.values():
            await ws.send_json(request)

    async def request_approval(self, scope: dict[str, Any], tool: str, summary: str, reason: str) -> bool:
        """Ask the user to approve a tool call; deny if nobody answers in time."""
        request_id = uuid.uuid4().hex
        request = {
            "type": "approval_request", "id": request_id, "tool": tool,
            "summary": summary, "reason": reason, **scope,
        }
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = (future, request)
        await self.publish(request)
        if self.notifier:
            await self.notifier.send("Approval needed", f"The agent wants to {reason}.", priority="high")
        try:
            return await asyncio.wait_for(future, APPROVAL_TIMEOUT_S)
        except TimeoutError:
            return False
        finally:
            self.pending.pop(request_id, None)
            await self.publish({"type": "approval_resolved", "id": request_id})

    def resolve(self, request_id: str, allow: bool) -> None:
        if entry := self.pending.get(request_id):
            future, _ = entry
            if not future.done():
                future.set_result(allow)
