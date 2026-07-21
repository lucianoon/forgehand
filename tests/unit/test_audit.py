from __future__ import annotations

import pytest

from app.infrastructure.audit import AuditEvent, JsonlAuditLog


@pytest.mark.asyncio
async def test_jsonl_audit_survives_new_instance(tmp_path):
    path = tmp_path / "audit.jsonl"
    first = JsonlAuditLog(str(path), max_events=50)
    await first.record(
        AuditEvent(
            timestamp="2026-07-20T00:00:00+00:00",
            action="workflow_create",
            outcome="accepted",
            client_id="client-a",
        )
    )

    restarted = JsonlAuditLog(str(path), max_events=50)
    events = await restarted.list_recent(client_id="client-a")

    assert len(events) == 1
    assert events[0].action == "workflow_create"
