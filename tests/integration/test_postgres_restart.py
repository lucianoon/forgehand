"""O teste que justifica o Postgres: workflow pausa no human_gate,
o PROCESSO morre (container novo, zero memoria compartilhada), e a
decisao humana retoma do checkpoint no banco. Auditoria via SQL no final."""

import asyncio
import json
import os
import sys
from dataclasses import replace
from uuid import uuid4

sys.path.insert(0, ".")
import anthropic
import httpx
import pytest
from app.api.container import build_container, checkpointer_context
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import workflow_queue_context
from app.models.task import TaskStatus


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
                        "description": "CRUD",
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
    if "files" in props:
        return tool_result({"summary": "ok", "files": [], "notes": []}, model)
    if "criteria" in props:
        title = user.split("Tarefa: ")[1].split("\n")[0]
        if title == "backend":  # rejeita sempre -> escala -> gate
            return tool_result(
                {
                    "criteria": [{"criterion": "c", "score": 0.2, "reasoning": "r"}],
                    "failures": ["f"],
                    "required_changes": ["refazer"],
                    "overall_score": 0.2,
                    "approved": False,
                },
                model,
            )
        return tool_result(
            {
                "criteria": [{"criterion": "c", "score": 0.9, "reasoning": "r"}],
                "failures": [],
                "required_changes": [],
                "overall_score": 0.9,
                "approved": True,
            },
            model,
        )


SETTINGS = Settings(
    checkpointer_backend="postgres",
    workflow_queue_backend="postgres",
    llm_provider_backend="anthropic",
    repository_grounding_enabled=False,
    executor_apply_files_enabled=False,
    database_url=os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://forge:forge@localhost:5432/agent_forge",
    ),
    api_keys_json=json.dumps(
        {"key-demo": {"client_id": "client-demo", "projects": ["demo"]}}
    ),
)


def make_client():
    return anthropic.AsyncAnthropic(
        api_key="t",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="defina RUN_POSTGRES_TESTS=1 com PostgreSQL local disponível",
)
async def test_postgres_restart():
    # ---- Processo 1: cria workflow, chega ao gate, MORRE ----
    async with checkpointer_context(SETTINGS) as cp1:
        async with workflow_queue_context(SETTINGS) as q1:
            c1 = build_container(
                SETTINGS, cp1, q1, run_workers=True, anthropic_client=make_client()
            )
            wid = await c1.workflow_service.start(
                "demo", "Crie uma API FastAPI de clientes", None, "client-demo"
            )
            for _ in range(100):
                await asyncio.sleep(0.1)
                st = await c1.workflow_service.get(wid)
                if st["pending_decision"]:
                    break
            assert st["pending_decision"]["reason"] == "tasks_escalated"
            print(
                f"1. processo 1 OK — workflow {wid[:8]}... pausado no gate, checkpoint no Postgres"
            )
            await c1.workflow_service.shutdown()
    # context manager fechou: pool encerrado, "processo morto"

    # ---- Processo 2: instancia NOVA, mesmo banco ----
    async with checkpointer_context(SETTINGS) as cp2:
        async with workflow_queue_context(SETTINGS) as q2:
            c2 = build_container(
                SETTINGS, cp2, q2, run_workers=True, anthropic_client=make_client()
            )
            st2 = await c2.workflow_service.get(wid)
            assert st2["pending_decision"] is not None, (
                "interrupt deveria sobreviver ao restart"
            )
            backend = next(t for t in st2["tasks"] if t["title"] == "backend")
            assert (
                backend["status"] == TaskStatus.ESCALATED and backend["attempts"] == 3
            )
            print(
                "2. restart OK — processo novo leu interrupt pendente e estado completo do banco"
            )

            await c2.workflow_service.decide(wid, "accept_partial")
            for _ in range(100):
                await asyncio.sleep(0.1)
                st3 = await c2.workflow_service.get(wid)
                if st3["phase"].value == "completed":
                    break
            assert "PARCIAL" in st3["final_output"]
            print(
                "3. retomada OK — decisão humana em outro processo concluiu o workflow"
            )
            await c2.workflow_service.shutdown()

    # ---- Auditoria: checkpoints consultaveis por SQL (regra 10) ----
    import psycopg

    with psycopg.connect(SETTINGS.database_url) as conn:
        n = conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (wid,)
        ).fetchone()[0]
        assert n > 5, f"esperava trilha de checkpoints, achou {n}"
        print(f"4. auditoria OK — {n} checkpoints do workflow consultáveis via SQL")

    print("\n4/4 cenarios postgres OK")


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="defina RUN_POSTGRES_TESTS=1 com PostgreSQL local disponível",
)
async def test_postgres_queue_heartbeat_and_lease_ownership():
    lease_settings = SETTINGS.model_copy(update={"workflow_queue_lease_seconds": 0.2})
    workflow_id = f"lease-{uuid4()}"

    async with workflow_queue_context(lease_settings) as queue:
        await queue.enqueue(
            workflow_id=workflow_id,
            project_id="demo",
            owner_client_id="client-demo",
            kind="start",
            payload={"workflow_id": workflow_id},
        )
        delivery = await queue.dequeue("worker-a", 0.01)
        assert delivery is not None

        for _ in range(3):
            await asyncio.sleep(0.1)
            assert await queue.heartbeat(delivery) is True

        assert await queue.dequeue("worker-b", 0.01) is None
        assert await queue.acknowledge(replace(delivery, locked_by="worker-b")) is False
        assert await queue.acknowledge(delivery) is True
