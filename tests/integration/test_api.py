"""API e2e: HTTP -> grafo -> checkpointer -> HTTP, com Anthropic mockado."""

import asyncio
import json
import sys
from unittest.mock import AsyncMock

sys.path.insert(0, ".")
import anthropic
import httpx
import pytest
from langgraph.checkpoint.memory import MemorySaver
from fastapi import FastAPI
from app.api.container import build_container
from app.api.routes.operations import router as operations_router
from app.api.routes.workflows import router as workflows_router
from app.graph.workflow import build_serde
from app.infrastructure.settings import Settings
from app.infrastructure.telemetry import HttpMetrics
from app.infrastructure.workflow_queue import InMemoryWorkflowQueue

REJECT_BACKEND_ALWAYS = {"on": False}
API_KEYS_JSON = json.dumps(
    {
        "key-demo": {"client_id": "client-demo", "projects": ["demo"]},
        "key-other": {"client_id": "client-other", "projects": ["other"]},
        "key-viewer": {
            "client_id": "client-viewer",
            "projects": ["demo"],
            "role": "viewer",
        },
    }
)


def tool_result(payload, model):
    return httpx.Response(
        200,
        json={
            "id": "m",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [
                {
                    "type": "tool_use",
                    "id": "t",
                    "name": "emit_structured_output",
                    "input": payload,
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 500, "output_tokens": 300},
        },
    )


def handler(request):
    body = json.loads(request.content)
    props = body["tools"][0]["input_schema"].get("properties", {})
    model, user = body["model"], body["messages"][-1]["content"]
    if "rationale" in props:
        return tool_result(
            {
                "rationale": "mvp",
                "tasks": [
                    {
                        "title": "backend",
                        "description": "CRUD clientes",
                        "capability": "backend",
                        "acceptance_criteria": ["CRUD completo"],
                        "depends_on": [],
                        "is_critical": False,
                    },
                    {
                        "title": "docs",
                        "description": "README",
                        "capability": "documentation",
                        "acceptance_criteria": ["README ok"],
                        "depends_on": [],
                        "is_critical": False,
                    },
                ],
            },
            model,
        )
    if "operations" in props:
        title = user.split("Tarefa: ")[1].split("\n")[0]
        return tool_result(
            {
                "summary": f"{title} feito",
                "operations": [
                    {"op": "create", "path": f"{title}.py", "content": "# ok"}
                ],
                "notes": [],
            },
            model,
        )
    if "criteria" in props:
        title = user.split("Tarefa: ")[1].split("\n")[0]
        if REJECT_BACKEND_ALWAYS["on"] and title == "backend":
            return tool_result(
                {
                    "criteria": [
                        {
                            "criterion": "CRUD completo",
                            "score": 0.2,
                            "reasoning": "incompleto",
                        }
                    ],
                    "failures": ["falta tudo"],
                    "required_changes": ["refazer"],
                    "overall_score": 0.2,
                    "approved": False,
                },
                model,
            )
        return tool_result(
            {
                "criteria": [{"criterion": "ok", "score": 0.9, "reasoning": "ok"}],
                "failures": [],
                "required_changes": [],
                "overall_score": 0.9,
                "approved": True,
            },
            model,
        )
    if "diagnosis" in props:
        return tool_result(
            {
                "diagnosis": "backend segue incompleto após as tentativas",
                "guidance": ["Implementar o CRUD completo antes de retornar"],
                "escalate_tier": False,
            },
            model,
        )
    raise AssertionError(f"schema inesperado: {list(props)}")


def make_app(run_workers=True, factory_mode_enabled=False):
    REJECT_BACKEND_ALWAYS["on"] = False
    settings = Settings(
        checkpointer_backend="memory",
        workflow_queue_backend="memory",
        llm_provider_backend="anthropic",
        repository_grounding_enabled=False,
        executor_apply_files_enabled=False,
        api_keys_json=API_KEYS_JSON,
        factory_mode_enabled=factory_mode_enabled,
    )
    client = anthropic.AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    container = build_container(
        settings,
        MemorySaver(serde=build_serde()),
        InMemoryWorkflowQueue(),
        run_workers=run_workers,
        anthropic_client=client,
    )
    app = FastAPI()
    app.include_router(operations_router)
    app.include_router(workflows_router)
    app.state.settings = settings
    app.state.container = container
    app.state.http_metrics = HttpMetrics()
    if run_workers:
        container.workflow_service.start_workers()
    return app


@pytest.mark.asyncio
async def test_workflow_history_survives_polling_and_preserves_owner_scope():
    from app.infrastructure.audit import build_audit_event

    app = make_app(run_workers=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "key-demo"},
    ) as client:
        created = await client.post(
            "/workflows",
            json={
                "project_id": "demo",
                "request": "Crie testes para o projeto de exemplo",
            },
        )
        assert created.status_code == 202
        wid = created.json()["workflow_id"]
        await app.state.container.job_queue.enqueue(
            workflow_id="other-owner",
            project_id="demo",
            owner_client_id="someone-else",
            kind="start",
            payload={},
        )
        for _ in range(600):
            await app.state.container.audit_log.record(
                build_audit_event(
                    action="workflow_get",
                    outcome="success",
                    client_id="client-demo",
                    project_id="demo",
                    workflow_id=wid,
                )
            )
        assert (await client.get(f"/workflows/{wid}")).status_code == 200
        response = await client.get("/workflows", params={"project_id": "demo"})
        assert response.status_code == 200
        assert [w["workflow_id"] for w in response.json()] == [wid]


@pytest.mark.asyncio
async def test_manual_pr_endpoint_cannot_bypass_factory_delivery(monkeypatch):
    app = make_app(run_workers=False)
    service = app.state.container.workflow_service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-API-Key": "key-demo"}
    ) as client:
        created = await client.post(
            "/workflows",
            json={
                "project_id": "demo",
                "request": "corrigir o comportamento do widget",
            },
        )
        workflow_id = created.json()["workflow_id"]
        monkeypatch.setattr(
            service,
            "get_details",
            AsyncMock(
                return_value={
                    "work_order": {"id": "factory-order"},
                }
            ),
        )
        response = await client.post(
            f"/workflows/{workflow_id}/pull-request",
            json={
                "repository": "acme/widget",
                "head_branch": "main",
            },
        )
    assert response.status_code == 409
    assert "fluxo validado" in response.json()["detail"]


@pytest.mark.asyncio
async def test_direct_factory_order_is_normalized_before_queueing():
    app = make_app(run_workers=False, factory_mode_enabled=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-API-Key": "key-demo"},
    ) as client:
        response = await client.post(
            "/workflows",
            json={
                "project_id": "demo",
                "work_order": {
                    "repository": "acme/widgets",
                    "base_ref": "develop",
                    "requested_outcome": "Corrigir o cálculo do total do pedido.",
                    "acceptance_criteria": ["Teste de regressão passa"],
                    "build_profile": "python",
                    "idempotency_key": "issue-7",
                },
            },
        )
        status_response = await client.get(
            f"/workflows/{response.json()['workflow_id']}"
        )

    assert response.status_code == 202, response.text
    assert status_response.json()["provenance"]["repository"]["full_name"] == (
        "acme/widgets"
    )
    job = await app.state.container.job_queue.dequeue("test-worker", 0.01)
    assert job is not None
    order = job.payload["work_order"]
    assert order.repository.full_name == "acme/widgets"
    assert order.repository.base_ref == "develop"
    assert order.source.kind == "direct"
    assert job.payload["delivery"].repository == "acme/widgets"
    assert job.payload["budget"].max_tokens == 500_000
    events = await app.state.container.audit_log.list_recent(limit=20)
    assert any(
        event.action == "work_order_intake" and event.outcome == "accepted"
        for event in events
    )
    await app.state.container.workflow_service.shutdown()


@pytest.mark.asyncio
async def test_issue_factory_order_reads_snapshot_before_queueing(monkeypatch):
    from app.models.factory import GitHubIssueSnapshot
    from app.api.routes import workflows
    from datetime import datetime, timezone

    class IssueClient:
        def __init__(self, **kwargs):
            pass

        async def fetch_issue_snapshot(self, **kwargs):
            assert kwargs["repository"] == "acme/widgets"
            return GitHubIssueSnapshot(
                url=kwargs["source_url"],
                number=7,
                title="Fix total calculation",
                repository="acme/widgets",
                author="operator",
                updated_at=datetime.now(timezone.utc),
            )

        async def close(self):
            pass

    monkeypatch.setattr(workflows, "GitHubSCMClient", IssueClient)
    monkeypatch.setattr(workflows, "build_token_provider_from_env", lambda: object())
    app = make_app(run_workers=False, factory_mode_enabled=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "key-demo"},
    ) as client:
        created = await client.post(
            "/workflows",
            json={
                "project_id": "demo",
                "work_order": {
                    "issue_url": "https://github.com/acme/widgets/issues/7",
                    "acceptance_criteria": ["Pass regression"],
                },
            },
        )
        assert created.status_code == 202, created.text
        state = await client.get(f"/workflows/{created.json()['workflow_id']}")
        assert state.json()["provenance"]["source"]["snapshot"]["number"] == 7
        rejected = await client.post(
            "/workflows",
            json={
                "project_id": "demo",
                "work_order": {
                    "issue_url": "http://127.0.0.1/secrets",
                    "acceptance_criteria": ["Pass"],
                },
            },
        )
        assert rejected.status_code == 422
    await app.state.container.workflow_service.shutdown()


@pytest.mark.asyncio
async def test_factory_order_is_rejected_when_disabled_or_ambiguous():
    app = make_app(run_workers=False)
    transport = httpx.ASGITransport(app=app)
    work_order = {
        "repository": "acme/widgets",
        "requested_outcome": "Corrigir o cálculo do total do pedido.",
        "acceptance_criteria": ["Teste de regressão passa"],
    }
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-API-Key": "key-demo"},
    ) as client:
        disabled = await client.post(
            "/workflows", json={"project_id": "demo", "work_order": work_order}
        )
        ambiguous = await client.post(
            "/workflows",
            json={
                "project_id": "demo",
                "request": "Uma solicitação legada válida",
                "work_order": work_order,
            },
        )

    assert disabled.status_code == 409
    assert ambiguous.status_code == 422
    assert (await app.state.container.job_queue.get_stats()).queued == 0
    events = await app.state.container.audit_log.list_recent(limit=20)
    assert any(
        event.action == "work_order_intake" and event.outcome == "factory_disabled"
        for event in events
    )
    await app.state.container.workflow_service.shutdown()


@pytest.mark.asyncio
async def test_factory_order_idempotency_is_scoped_and_deduplicates_queue():
    app = make_app(run_workers=False, factory_mode_enabled=True)
    transport = httpx.ASGITransport(app=app)
    payload = {
        "project_id": "demo",
        "work_order": {
            "repository": "acme/widgets",
            "requested_outcome": "Corrigir o cálculo do total do pedido.",
            "acceptance_criteria": ["Teste de regressão passa"],
            "idempotency_key": "customer-request-42",
        },
    }
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-API-Key": "key-demo"},
    ) as client:
        first = await client.post("/workflows", json=payload)
        second = await client.post("/workflows", json=payload)

    assert first.status_code == second.status_code == 202
    assert first.json()["workflow_id"] == second.json()["workflow_id"]
    assert (await app.state.container.job_queue.get_stats()).queued == 1
    await app.state.container.workflow_service.shutdown()


@pytest.mark.asyncio
async def test_cancel_queued_workflow_through_api():
    app = make_app(run_workers=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-API-Key": "key-demo"},
    ) as client:
        created = await client.post(
            "/workflows",
            json={
                "project_id": "demo",
                "request": "Crie uma API FastAPI de gerenciamento de clientes",
            },
        )
        workflow_id = created.json()["workflow_id"]
        cancelled = await client.post(f"/workflows/{workflow_id}/cancel")
        state = await client.get(f"/workflows/{workflow_id}")

    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "cancelled"
    assert state.status_code == 200
    assert state.json()["status"] == "cancelled"
    await app.state.container.workflow_service.shutdown()


async def poll(client, wid, until, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        r = await client.get(f"/workflows/{wid}")
        if r.status_code == 200 and r.json()["status"] in until:
            return r.json()
        await asyncio.sleep(0.05)
    raise TimeoutError(f"workflow {wid} nao chegou em {until}")


@pytest.mark.asyncio
async def test_api_e2e():
    app = make_app()
    transport = httpx.ASGITransport(app=app)
    auth = {"X-API-Key": "key-demo"}
    wrong_auth = {"X-API-Key": "key-other"}
    invalid_auth = {"X-API-Key": "bad-key"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
        unauthorized = await anon.post(
            "/workflows",
            json={
                "project_id": "demo",
                "request": "Crie uma API FastAPI de gerenciamento de clientes",
            },
        )
        assert unauthorized.status_code == 401
        invalid = await anon.post(
            "/workflows",
            headers=invalid_auth,
            json={
                "project_id": "demo",
                "request": "Crie uma API FastAPI de gerenciamento de clientes",
            },
        )
        assert invalid.status_code == 401

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=auth
    ) as client:
        # ---- 1. Happy path: POST 202 -> poll -> completed ----
        r = await client.post(
            "/workflows",
            json={
                "project_id": "demo",
                "request": "Crie uma API FastAPI de gerenciamento de clientes",
                "acceptance_criteria": ["Possuir CRUD de clientes", "Usar PostgreSQL"],
            },
        )
        assert r.status_code == 202, r.text
        wid = r.json()["workflow_id"]
        assert r.json()["status"] == "running"
        assert r.json()["current_stage"] == "queued"
        final = await poll(client, wid, {"completed"})
        assert final["current_stage"] == "completed"
        assert all(t["status"] == "completed" for t in final["tasks"])
        assert final["usage"]["tokens"] > 0
        assert final["final_output"] and "backend" in final["final_output"]
        history = await client.get("/workflows", params={"project_id": "demo"})
        assert history.status_code == 200
        assert history.json()[0]["workflow_id"] == wid
        prometheus = await client.get("/metrics/prometheus")
        assert prometheus.status_code == 200
        assert "forgehand_queue_queued" in prometheus.text
        print("1. auth + happy path OK — 401 sem key, 202 com key válida")

        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers=wrong_auth
        ) as forbidden_client:
            forbidden_get = await forbidden_client.get(f"/workflows/{wid}")
            assert forbidden_get.status_code == 403

            forbidden_create = await forbidden_client.post(
                "/workflows",
                json={
                    "project_id": "demo",
                    "request": "Crie uma API FastAPI de gerenciamento de clientes",
                },
            )
            assert forbidden_create.status_code == 403

        # ---- 2. 404 e 422 ----
        assert (await client.get("/workflows/nao-existe")).status_code == 404
        assert (
            await client.post(
                "/workflows", json={"project_id": "x", "request": "curto"}
            )
        ).status_code == 422
        print("2. erros OK — 404 p/ id desconhecido, 422 p/ payload inválido")

        # ---- 3. Escalation -> awaiting_decision -> decisao humana via API ----
        REJECT_BACKEND_ALWAYS["on"] = True
        r3 = await client.post(
            "/workflows",
            json={
                "project_id": "demo",
                "request": "Crie uma API FastAPI de clientes v2",
            },
        )
        wid3 = r3.json()["workflow_id"]
        waiting = await poll(client, wid3, {"awaiting_decision"})
        pd = waiting["pending_decision"]
        assert pd["reason"] == "tasks_escalated" and "accept_partial" in pd["options"]
        backend = next(t for t in waiting["tasks"] if t["title"] == "backend")
        assert backend["status"] == "escalated" and backend["attempts"] == 3
        print("3. gate humano OK — awaiting_decision exposto com payload do interrupt")

        # decisao invalida no estado errado -> validada; decisao correta -> retoma
        r_bad = await client.post(
            f"/workflows/{wid}/decision", json={"decision": "retry"}
        )
        assert r_bad.status_code == 409, "workflow completo nao aceita decisao"
        r_dec = await client.post(
            f"/workflows/{wid3}/decision", json={"decision": "accept_partial"}
        )
        assert r_dec.status_code == 202
        final3 = await poll(client, wid3, {"completed"})
        assert "PARCIAL" in final3["final_output"]
        docs = next(t for t in final3["tasks"] if t["title"] == "docs")
        assert docs["status"] == "completed"
        print(
            "4. decisão via API OK — 409 sem interrupt pendente, accept_partial entrega parcial"
        )

        # ---- 5. Decisao com valor fora do contrato -> 422 ----
        assert (
            await client.post(
                f"/workflows/{wid3}/decision", json={"decision": "talvez"}
            )
        ).status_code == 422
        print("5. contrato de decisão OK — literal validado no schema")

    await app.state.container.workflow_service.shutdown()
    print("\n5/5 cenarios de API OK")


@pytest.mark.asyncio
async def test_operational_endpoints_expose_health_readiness_and_metrics():
    app = make_app()
    transport = httpx.ASGITransport(app=app)
    auth = {"X-API-Key": "key-demo"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        ready = await client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        assert ready.json()["queue_ready"] is True

        metrics_before = await client.get("/metrics")
        assert metrics_before.status_code == 200
        assert metrics_before.json()["queue"]["backend"] == "memory"
        assert metrics_before.json()["queue"]["queued"] == 0
        assert metrics_before.json()["audit"]["stored_events"] >= 0

        create = await client.post(
            "/workflows",
            headers=auth,
            json={
                "project_id": "demo",
                "request": "Crie uma API FastAPI de gerenciamento de clientes",
            },
        )
        assert create.status_code == 202
        workflow_id = create.json()["workflow_id"]

        metrics_during = await client.get("/metrics")
        assert metrics_during.status_code == 200
        delivered_total = metrics_during.json()["queue"]["delivered_total"]
        for _ in range(20):
            if delivered_total >= 1:
                break
            await asyncio.sleep(0.02)
            metrics_during = await client.get("/metrics")
            delivered_total = metrics_during.json()["queue"]["delivered_total"]
        assert delivered_total >= 1

        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers=auth
        ) as authed:
            await poll(authed, workflow_id, {"completed"})

        metrics_after = await client.get("/metrics")
        assert metrics_after.status_code == 200
        assert metrics_after.json()["queue"]["done"] >= 1
        assert metrics_after.json()["workers"]["embedded_workers_enabled"] is True

    await app.state.container.workflow_service.shutdown()


@pytest.mark.asyncio
async def test_rbac_blocks_viewer_mutations_and_admin_audit():
    app = make_app()
    transport = httpx.ASGITransport(app=app)
    viewer = {"X-API-Key": "key-viewer"}

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=viewer
    ) as client:
        create = await client.post(
            "/workflows",
            json={"project_id": "demo", "request": "Analise o projeto existente"},
        )
        audit = await client.get("/audit/events")

    assert create.status_code == 403
    assert audit.status_code == 403
    await app.state.container.workflow_service.shutdown()


@pytest.mark.asyncio
async def test_audit_events_are_recorded_and_filtered_by_client():
    app = make_app()
    transport = httpx.ASGITransport(app=app)
    auth = {"X-API-Key": "key-demo"}
    other_auth = {"X-API-Key": "key-other"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
        assert (
            await anon.post(
                "/workflows",
                json={
                    "project_id": "demo",
                    "request": "Crie uma API FastAPI de gerenciamento de clientes",
                },
            )
        ).status_code == 401

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=auth
    ) as client:
        created = await client.post(
            "/workflows",
            json={
                "project_id": "demo",
                "request": "Crie uma API FastAPI de gerenciamento de clientes",
            },
        )
        assert created.status_code == 202
        workflow_id = created.json()["workflow_id"]

        await poll(client, workflow_id, {"completed"})

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=other_auth
    ) as other_client:
        forbidden_get = await other_client.get(f"/workflows/{workflow_id}")
        assert forbidden_get.status_code == 403
        other_events = await other_client.get("/audit/events?limit=20")
        assert other_events.status_code == 200
        other_payload = other_events.json()
        assert other_payload["client_id"] == "client-other"
        assert any(
            event["action"] == "workflow_access" and event["outcome"] == "forbidden"
            for event in other_payload["events"]
        )

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=auth
    ) as client:
        events = await client.get("/audit/events?limit=50")
        assert events.status_code == 200
        payload = events.json()
        assert payload["client_id"] == "client-demo"
        assert any(
            event["action"] == "auth" and event["outcome"] == "missing_api_key"
            for event in payload["events"]
        )
        assert any(
            event["action"] == "workflow_create" and event["outcome"] == "accepted"
            for event in payload["events"]
        )
        assert any(
            event["action"] == "workflow_get" and event["outcome"] == "success"
            for event in payload["events"]
        )
        assert all(
            event["client_id"] in {None, "client-demo"} for event in payload["events"]
        )

    await app.state.container.workflow_service.shutdown()
