"""O teste que justifica o Neo4j: a memória do projeto sobrevive ao processo.

Uma instância persiste o desfecho do workflow; uma instância NOVA (driver
novo, zero memória compartilhada) carrega o histórico do mesmo projeto.
"""

import os
from uuid import uuid4

import pytest

from app.graph.state import WorkflowPhase, WorkflowState
from app.infrastructure.memory import project_memory_context
from app.infrastructure.settings import Settings
from app.models.task import AgentTask, Capability, TaskStatus


def make_settings() -> Settings:
    return Settings(
        memory_backend="neo4j",
        neo4j_uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        repository_grounding_enabled=False,
        memory_recent_workflows_limit=5,
    )


@pytest.mark.skipif(
    os.getenv("RUN_NEO4J_TESTS") != "1",
    reason="defina RUN_NEO4J_TESTS=1 com Neo4j local disponível",
)
@pytest.mark.asyncio
async def test_memory_survives_process_restart():
    settings = make_settings()
    project_id = f"proj-{uuid4()}"
    workflow_id = f"wf-{uuid4()}"
    task = AgentTask(
        title="backend",
        description="d",
        capability=Capability.BACKEND,
        acceptance_criteria=["ok"],
        status=TaskStatus.COMPLETED,
    )
    state = WorkflowState(
        request="Crie uma API de clientes",
        project_id=project_id,
        workflow_id=workflow_id,
        owner_client_id="c",
        plan=[task],
        usage={"tokens": 42, "cost_usd": 0.02},
        phase=WorkflowPhase.COMPLETED,
        final_output="entrega",
    )

    async with project_memory_context(settings) as memory:
        await memory.persist(state)

    # "restart": contexto novo, driver novo — só o banco em comum
    async with project_memory_context(settings) as fresh_memory:
        context = await fresh_memory.load_context(project_id)

    assert context["last_workflow_id"] == workflow_id
    recent = context["project_memory"]["recent_workflows"]
    assert recent[0]["phase"] == "completed"
    assert recent[0]["tasks"] == [{"title": "backend", "status": "completed"}]
