from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.graph.state import WorkflowPhase
from app.graph.workflow import build_serde, build_workflow
from app.models.factory import (
    DirectWorkOrderSource,
    FactoryStage,
    RepositoryTarget,
    WorkOrder,
    WorkspaceLease,
    WorkspaceLifecycle,
)
from app.models.task import AgentTask, Capability, EvaluationResult


class RecordingMemory:
    def __init__(self) -> None:
        self.persisted = []

    async def load_context(self, project_id: str, request: str = "") -> dict:
        return {"repository_grounding": {"repo_root": "/legacy"}}

    async def persist(self, state) -> None:
        self.persisted.append(state)


class Planner:
    async def create_plan(self, request: str, context: dict):
        return [
            AgentTask(
                title="alterar",
                description="alterar arquivo no workspace isolado",
                capability=Capability.BACKEND,
                acceptance_criteria=["ok"],
            )
        ]


class Executor:
    def __init__(self) -> None:
        self.contexts: list[dict] = []

    async def execute(self, task, context):
        self.contexts.append(context)
        return {
            "result": {"ok": True},
            "agent": "lease-executor",
            "model": "fake",
            "tokens": 1,
            "cost_usd": 0.0,
        }


class Registry:
    def __init__(self, executor: Executor) -> None:
        self.executor = executor

    def select(self, task):
        return self.executor


class Judge:
    async def evaluate(self, task, context):
        return EvaluationResult(
            task_id=task.id,
            approved=True,
            score=1,
            criteria_scores={"ok": 1},
        )


class FactoryRuntime:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.executor = Executor()
        self.registry = Registry(self.executor)
        self.leases: list[WorkspaceLease] = []

    def build_grounding(self, lease: WorkspaceLease, request: str) -> dict:
        self.leases.append(lease)
        return {
            "repo_root": str(self.workspace),
            "require_citations": False,
            "evidence": [],
        }

    def build_planner(self, lease: WorkspaceLease):
        self.leases.append(lease)
        return Planner()

    def build_registry(self, lease: WorkspaceLease):
        self.leases.append(lease)
        return self.registry

    def build_judge(self, lease: WorkspaceLease):
        self.leases.append(lease)
        return Judge()


class WorkspaceManager:
    def __init__(self, lease: WorkspaceLease, *, fail: bool = False) -> None:
        self.lease = lease
        self.fail = fail
        self.provisions = 0
        self.reconstructions = 0

    async def provision(self, workflow_id: str, order: WorkOrder) -> WorkspaceLease:
        self.provisions += 1
        if self.fail:
            raise RuntimeError("clone denied")
        return self.lease

    async def reconstruct(self, lease: WorkspaceLease) -> WorkspaceLease:
        self.reconstructions += 1
        return lease


def work_order() -> WorkOrder:
    return WorkOrder(
        source=DirectWorkOrderSource(),
        repository=RepositoryTarget(full_name="acme/widget"),
        requested_outcome="corrigir o comportamento do widget",
        acceptance_criteria=["testes passam"],
    )


def lease(workspace: Path, workflow_id: str) -> WorkspaceLease:
    return WorkspaceLease(
        workflow_id=workflow_id,
        repository=RepositoryTarget(full_name="acme/widget"),
        local_path=str(workspace),
        branch=f"forgehand/{workflow_id}",
        base_sha="a" * 40,
        state=WorkspaceLifecycle.READY,
    )


def graph(manager, runtime, memory):
    return build_workflow(
        Planner(),
        Registry(Executor()),
        Judge(),
        memory,
        MemorySaver(serde=build_serde()),
        workspace_manager=manager,
        runtime_factory=runtime,
    )


@pytest.mark.asyncio
async def test_factory_graph_provisions_and_binds_all_roles_to_lease(tmp_path: Path):
    workflow_id = "factory-bindings"
    active_lease = lease(tmp_path, workflow_id)
    manager = WorkspaceManager(active_lease)
    runtime = FactoryRuntime(tmp_path)
    memory = RecordingMemory()

    output = await graph(manager, runtime, memory).ainvoke(
        {
            "request": "corrigir o comportamento do widget",
            "project_id": "acme-widget",
            "workflow_id": workflow_id,
            "owner_client_id": "client",
            "work_order": work_order(),
        },
        {"configurable": {"thread_id": workflow_id}},
    )

    assert output["phase"] == WorkflowPhase.COMPLETED
    assert output["workspace"].local_path == str(tmp_path)
    assert output["factory_stage"] == FactoryStage.IMPLEMENTATION
    assert manager.provisions == 1
    assert runtime.leases and all(
        item.local_path == str(tmp_path) for item in runtime.leases
    )
    assert runtime.executor.contexts[0]["repository_grounding"]["repo_root"] == str(
        tmp_path
    )


@pytest.mark.asyncio
async def test_factory_graph_reconstructs_checkpointed_lease(tmp_path: Path):
    workflow_id = "factory-resume"
    active_lease = lease(tmp_path, workflow_id)
    manager = WorkspaceManager(active_lease)
    runtime = FactoryRuntime(tmp_path)

    await graph(manager, runtime, RecordingMemory()).ainvoke(
        {
            "request": "corrigir o comportamento do widget",
            "project_id": "acme-widget",
            "workflow_id": workflow_id,
            "owner_client_id": "client",
            "work_order": work_order(),
            "workspace": active_lease,
        },
        {"configurable": {"thread_id": workflow_id}},
    )

    assert manager.provisions == 0
    assert manager.reconstructions == 1


@pytest.mark.asyncio
async def test_factory_graph_routes_provisioning_failure_to_terminal_state(
    tmp_path: Path,
):
    workflow_id = "factory-provision-failure"
    manager = WorkspaceManager(lease(tmp_path, workflow_id), fail=True)
    memory = RecordingMemory()

    output = await graph(manager, FactoryRuntime(tmp_path), memory).ainvoke(
        {
            "request": "corrigir o comportamento do widget",
            "project_id": "acme-widget",
            "workflow_id": workflow_id,
            "owner_client_id": "client",
            "work_order": work_order(),
        },
        {"configurable": {"thread_id": workflow_id}},
    )

    assert output["phase"] == WorkflowPhase.FAILED
    assert output["factory_stage"] == FactoryStage.PROVISIONING
    assert "clone denied" in output["error"]
    assert len(memory.persisted) == 1
