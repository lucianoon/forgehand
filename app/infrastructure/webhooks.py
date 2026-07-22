from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

import httpx


class WebhookDispatcher:
    def __init__(
        self,
        urls: list[str],
        signing_secret: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._urls = urls
        self._secret = signing_secret.encode()
        self._client = client or httpx.AsyncClient(timeout=10)

    async def publish(self, event: str, payload: dict[str, Any]) -> None:
        if not self._urls:
            return
        body = json.dumps(
            {"event": event, "payload": payload},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        signature = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        await asyncio.gather(
            *(
                self._client.post(
                    url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Forgehand-Event": event,
                        "X-Forgehand-Signature-256": f"sha256={signature}",
                    },
                )
                for url in self._urls
            ),
            return_exceptions=True,
        )

    async def close(self) -> None:
        await self._client.aclose()
