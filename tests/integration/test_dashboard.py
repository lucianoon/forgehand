from __future__ import annotations

import httpx
import pytest

from app.main import create_app


@pytest.mark.asyncio
async def test_dashboard_and_assets_are_served():
    app = create_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        dashboard = await client.get("/dashboard")
        script = await client.get("/dashboard/assets/app.js")
        styles = await client.get("/dashboard/assets/styles.css")

    assert dashboard.status_code == 200
    assert dashboard.headers["X-Request-ID"]
    assert dashboard.headers["X-Content-Type-Options"] == "nosniff"
    assert "Agent Forge · Mission Control" in dashboard.text
    assert 'id="workflow-form"' in dashboard.text
    assert 'id="workflow-history"' in dashboard.text
    assert 'id="cancel-workflow"' in dashboard.text
    assert 'data-decision="accept_partial"' in dashboard.text
    assert script.status_code == 200
    assert "sessionStorage" in script.text
    assert "refreshHistory" in script.text
    assert styles.status_code == 200
    assert "--green:" in styles.text
