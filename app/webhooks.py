"""Signed webhook delivery with retries and an inspectable delivery log."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import httpx

from app.config import Settings
from app.engine import Event, new_id
from app.signing import webhook_signature


class WebhookDispatcher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        self.log: list[dict[str, Any]] = []

    def reset(self) -> None:
        with self._lock:
            self.log.clear()

    def envelope(self, event: Event) -> dict[str, Any]:
        return {
            "entity": "event",
            "account_id": self.settings.account_id,
            "event": event.name,
            "contains": event.contains,
            "payload": {name: {"entity": entity} for name, entity in event.entities.items()},
            "created_at": int(time.time()),
        }

    async def dispatch(self, events: list[Event], duplicates: int = 1) -> None:
        for event in events:
            event_id = new_id("evt_")
            body = json.dumps(self.envelope(event), separators=(",", ":")).encode()
            for _ in range(max(1, duplicates)):
                await self._deliver(event.name, event_id, body)

    async def _deliver(self, name: str, event_id: str, body: bytes) -> None:
        signature = webhook_signature(body, self.settings.webhook_secret)
        record: dict[str, Any] = {
            "event_id": event_id,
            "event": name,
            "url": self.settings.webhook_url or None,
            "signature": signature,
            "body": json.loads(body),
            "attempts": 0,
            "delivered": False,
            "status_code": None,
            "error": None,
            "at": int(time.time()),
        }
        with self._lock:
            self.log.append(record)
        if not self.settings.webhook_url:
            record["error"] = "RZP_SIM_WEBHOOK_URL is not configured"
            return

        headers = {
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": event_id,
        }
        async with httpx.AsyncClient(timeout=self.settings.webhook_timeout_seconds) as client:
            for attempt in range(1, self.settings.webhook_retries + 1):
                record["attempts"] = attempt
                try:
                    response = await client.post(self.settings.webhook_url, content=body, headers=headers)
                    record["status_code"] = response.status_code
                    record["error"] = None
                    if 200 <= response.status_code < 300:
                        record["delivered"] = True
                        return
                except httpx.HTTPError as exc:
                    record["error"] = f"{type(exc).__name__}: {exc}"
                if attempt < self.settings.webhook_retries:
                    await asyncio.sleep(0.5 * attempt)
