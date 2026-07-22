from __future__ import annotations

import hashlib
import hmac

import httpx
import pytest

from app.infrastructure.webhooks import WebhookDispatcher


@pytest.mark.asyncio
async def test_webhook_dispatcher_signs_payload():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(202)

    dispatcher = WebhookDispatcher(
        ["https://hooks.test/forgehand"],
        "secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await dispatcher.publish("workflow.processed", {"workflow_id": "wf-1"})

    request = captured[0]
    expected = hmac.new(b"secret", request.content, hashlib.sha256).hexdigest()
    assert request.headers["X-Forgehand-Signature-256"] == f"sha256={expected}"
    assert request.headers["X-Forgehand-Event"] == "workflow.processed"
