"""Optional push notifications to your phone through ntfy. Sends short status text only."""

from __future__ import annotations

import asyncio
import urllib.request

from .config import Notifications


class Notifier:
    def __init__(self, settings: Notifications):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.ntfy_topic)

    async def send(self, title: str, message: str, priority: str = "default") -> None:
        if not self.enabled:
            return
        url = f"{self.settings.ntfy_server.rstrip('/')}/{self.settings.ntfy_topic}"
        request = urllib.request.Request(
            url, data=message.encode(), method="POST",
            headers={"Title": title.encode("ascii", "replace").decode(), "Priority": priority},
        )
        try:
            await asyncio.to_thread(urllib.request.urlopen, request, timeout=10)
        except Exception:
            pass  # a missed notification must never break an agent run
