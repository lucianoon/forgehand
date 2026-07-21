import pytest

from app.graph.state import WorkflowPhase, WorkflowState
from app.infrastructure.memory import (
    InMemoryProjectMemory,
    Neo4jProjectMemory,
    workflow_summary,
)
from app.infrastructure.settings import Settings
from app.models.task import AgentTask, Capability, TaskStatus


def make_settings(**overrides):
    defaults = {
        "repository_grounding_enabled": False,
        "memory_recent_workflows_limit": 2,
    }
    return Settings(**{**defaults, **overrides})


def make_state(workflow_id="wf-1", project_id="p", request="Crie uma API"):
    task = AgentTask(
        title="backend",
        description="d",
        capability=Capability.BACKEND,
        acceptance_criteria=["ok"],
        status=TaskStatus.COMPLETED,
    )
    return WorkflowState(
        request=request,
        project_id=project_id,
        workflow_id=workflow_id,
        owner_client_id="c",
        plan=[task],
        usage={"tokens": 10, "cost_usd": 0.01},
        phase=WorkflowPhase.COMPLETED,
        final_output="entrega final",
    )


class FakeRecord:
    def __init__(self, data):
        self._data = data

    def data(self):
        return dict(self._data)


class FakeResult:
    def __init__(self, records):
        self.records = records


class FakeDriver:
    def __init__(self, records=None, error=None):
        self.calls = []
        self._records = records or []
        self._error = error

    async def execute_query(self, query, parameters_=None, database_=None):
        self.calls.append(
            {"query": query, "parameters": parameters_, "database": database_}
        )
        if self._error is not None:
            raise self._error
        return FakeResult([FakeRecord(r) for r in self._records])


# --------------------------------------------------------------------------
# Contrato do backend em memória
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_memory_roundtrip_returns_recent_first_and_respects_limit():
    memory = InMemoryProjectMemory(make_settings())
    for i in range(3):  # limite é 2 — o mais antigo cai
        await memory.persist(make_state(workflow_id=f"wf-{i}"))

    context = await memory.load_context("p")

    assert context["last_workflow_id"] == "wf-2"
    recent = context["project_memory"]["recent_workflows"]
    assert len(recent) == 2
    assert recent[0]["phase"] == "completed"
    assert recent[0]["tasks"] == [{"title": "backend", "status": "completed"}]
    # projeção enxuta para o prompt: sem custo nem excerpt de saída
    assert "cost_usd" not in recent[0]
    assert "final_output_excerpt" not in recent[0]


@pytest.mark.asyncio
async def test_in_memory_isolates_projects():
    memory = InMemoryProjectMemory(make_settings())
    await memory.persist(make_state(project_id="a"))

    assert await memory.load_context("b") == {}


def test_workflow_summary_truncates_request_and_output():
    state = make_state(request="x" * 500)
    summary = workflow_summary(state)

    assert len(summary["request"]) == 200
    assert summary["tokens"] == 10
    assert summary["tasks"][0]["capability"] == "backend"


# --------------------------------------------------------------------------
# Backend Neo4j (driver injetado)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_neo4j_persist_merges_project_workflow_and_tasks():
    driver = FakeDriver()
    memory = Neo4jProjectMemory(driver, make_settings())

    await memory.persist(make_state())

    assert len(driver.calls) == 1
    call = driver.calls[0]
    assert "MERGE (p:Project {id: $project_id})" in call["query"]
    assert "MERGE (p)-[:HAS_WORKFLOW]->(w)" in call["query"]
    assert "MERGE (w)-[:EXECUTED]->(t)" in call["query"]
    assert call["parameters"]["project_id"] == "p"
    assert call["parameters"]["workflow_id"] == "wf-1"
    assert call["parameters"]["tasks"][0]["status"] == "completed"
    assert call["database"] == "neo4j"


@pytest.mark.asyncio
async def test_neo4j_load_context_maps_records_and_drops_null_tasks():
    driver = FakeDriver(
        records=[
            {
                "workflow_id": "wf-9",
                "request": "Crie uma API",
                "phase": "completed",
                "finished_at": "2026-07-20T00:00:00+00:00",
                # OPTIONAL MATCH sem tarefas produz {title: null}
                "tasks": [{"title": None, "status": None}],
            }
        ]
    )
    memory = Neo4jProjectMemory(driver, make_settings())

    context = await memory.load_context("p")

    assert context["last_workflow_id"] == "wf-9"
    recent = context["project_memory"]["recent_workflows"]
    assert recent[0]["tasks"] == []
    assert driver.calls[0]["parameters"] == {"project_id": "p", "limit": 2}


@pytest.mark.asyncio
async def test_neo4j_failures_degrade_without_raising():
    driver = FakeDriver(error=ConnectionError("bolt fora do ar"))
    memory = Neo4jProjectMemory(driver, make_settings())

    # memória é conselheira: leitura degrada para vazio, escrita não propaga
    assert await memory.load_context("p") == {}
    await memory.persist(make_state())
