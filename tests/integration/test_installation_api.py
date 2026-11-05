import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes.operations import router
from app.infrastructure.audit import InMemoryAuditLog
from app.infrastructure.settings import Settings


@pytest.mark.parametrize("ready", [True, False])
@pytest.mark.parametrize("key,expected", [(None, 401), ("invalid", 401), ("viewer", 403), ("approver", 403), ("admin", None)])
async def test_installation_diagnostics_require_admin_and_preserve_health_status(ready, key, expected):
    settings = Settings(_env_file=None, api_keys_json=json.dumps({
        role: {"client_id": role, "projects": ["*"], "role": role}
        for role in ("admin", "viewer", "approver")
    }))
    service = SimpleNamespace(installation_diagnostics=AsyncMock(return_value={"ready": ready, "configuration": {"workspace_root": "/private/control"}}))
    audit = InMemoryAuditLog()
    app = FastAPI()
    app.state.settings = settings
    app.state.container = SimpleNamespace(workflow_service=service, audit_log=audit)
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/operations/installation", headers={"X-API-Key": key} if key else {})
    assert response.status_code == (expected or (200 if ready else 503))
    if key == "admin":
        service.installation_diagnostics.assert_awaited_once()
        assert response.json()["ready"] is ready
        events = await audit.list_recent(limit=20)
        assert any(event.action == "installation_diagnostics" for event in events)
    else:
        service.installation_diagnostics.assert_not_awaited()
        assert "/private/control" not in response.text
