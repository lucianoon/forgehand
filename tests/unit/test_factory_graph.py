from pathlib import Path
import asyncio

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.graph.state import DeliveryResult, WorkflowBudget, WorkflowPhase
from app.api.service import WorkflowAlreadyTerminal, WorkflowService
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import InMemoryWorkflowQueue
from app.graph.workflow import build_serde, build_workflow
from app.factory.build_strategy import BuildProfileRegistry
from app.models.build import BuildPhase, BuildProfile
from app.models.build_execution import BuildOutcome, BuildPhaseResult, BuildRunResult
from app.models.factory import (
    BuildProfileSelection,
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
        self.tasks: list[AgentTask] = []

    async def execute(self, task, context):
        self.contexts.append(context)
        self.tasks.append(task)
        return {
            "result": {
                "ok": True,
                "workspace": {
                    "published_files": [{"path": "widget.py", "content": "ok\n"}],
                },
            },
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
    def __init__(self) -> None:
        self.contexts: list[dict] = []

    async def evaluate(self, task, context):
        self.contexts.append(context)
        return EvaluationResult(
            task_id=task.id,
            approved=True,
            score=1,
            criteria_scores={"ok": 1},
        )


class BuildRunner:
    def __init__(self, reports: list[BuildRunResult] | None = None) -> None:
        self.reports = reports or [successful_build()]
        self.calls = 0

    async def run(self, lease, selection):
        report = self.reports[min(self.calls, len(self.reports) - 1)]
        self.calls += 1
        return report.model_copy(
            update={
                "profile_name": selection.selected_profile,
                "profile_digest": selection.profile_digest,
            }
        )


@pytest.mark.asyncio
async def test_service_cancels_factory_validation_before_terminal_checkpoint(tmp_path):
    started, stopped = asyncio.Event(), asyncio.Event()

    class BlockingBuild(BuildRunner):
        async def run(self, lease, selection):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    publisher = Delivery()
    app = graph(
        WorkspaceManager(lease(tmp_path, "cancel")),
        FactoryRuntime(tmp_path),
        RecordingMemory(),
        delivery=publisher,
        build_runner=BlockingBuild(),
    )
    service = WorkflowService(app, Settings(), InMemoryWorkflowQueue(), False)
    invocation = asyncio.create_task(
        app.ainvoke(
            {
                "workflow_id": "cancel",
                "project_id": "p",
                "owner_client_id": "c",
                "request": "Corrigir o widget",
                "work_order": work_order(),
            },
            {"configurable": {"thread_id": "cancel"}},
        )
    )
    service._invocations["cancel"] = invocation
    await asyncio.wait_for(started.wait(), 2)
    await service.cancel("cancel")
    assert stopped.is_set()
    assert not publisher.calls
    assert (await service.get("cancel"))["phase"] == WorkflowPhase.CANCELLED


class Delivery:
    def __init__(self, ci_state: str = "success") -> None:
        self.ci_state = ci_state
        self.calls: list[dict] = []

    async def publish(self, **kwargs):
        self.calls.append(kwargs)
        return DeliveryResult(
            pull_request_number=23,
            url="https://github.com/acme/widget/pull/23",
            branch=kwargs["config"].head_branch,
            commit_sha="c" * 40,
            ci_state=self.ci_state,
            files=len(kwargs["files"]),
            failure_paths=["widget.py"] if self.ci_state == "failure" else [],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_factory_red_ci_repairs_or_exhausts_budget(tmp_path, exhausted):
    class CI(Delivery):
        async def publish(self, **kwargs):
            self.ci_state = "failure" if not self.calls or exhausted else "success"
            return await super().publish(**kwargs)

    runtime, publisher, runner = FactoryRuntime(tmp_path), CI(), BuildRunner()
    output = await graph(
        WorkspaceManager(lease(tmp_path, "ci-repair")),
        runtime,
        RecordingMemory(),
        delivery=publisher,
        build_runner=runner,
    ).ainvoke(
        {
            "workflow_id": "ci-repair",
            "project_id": "p",
            "owner_client_id": "c",
            "request": "Corrigir o widget",
            "work_order": work_order(),
            "budget": WorkflowBudget(max_iterations=1 if exhausted else 3),
        },
        {"configurable": {"thread_id": "ci-repair"}},
    )
    if exhausted:
        assert "__interrupt__" in output
        assert len(publisher.calls) == 2  # Initial delivery + one repair.
        assert output["iteration"] == 1
    else:
        assert output["phase"] == WorkflowPhase.READY_FOR_HUMAN_REVIEW
        assert len(publisher.calls) == 2
        assert runner.calls == 2
        assert publisher.calls[1]["config"].expected_head_sha == "c" * 40


@pytest.mark.asyncio
async def test_new_graph_instance_resumes_checkpoint_without_executing_agents_again(
    tmp_path,
):
    saver = MemorySaver(serde=build_serde())
    manager = WorkspaceManager(lease(tmp_path, "resume"))
    runtime, publisher, runner = (
        FactoryRuntime(tmp_path),
        Delivery("pending"),
        BuildRunner(),
    )
    config = {"configurable": {"thread_id": "resume"}}
    first = graph(
        manager,
        runtime,
        RecordingMemory(),
        delivery=publisher,
        build_runner=runner,
        checkpointer=saver,
    )
    paused = await first.ainvoke(
        {
            "workflow_id": "resume",
            "project_id": "p",
            "owner_client_id": "c",
            "request": "Corrigir o widget",
            "work_order": work_order(),
        },
        config,
    )
    assert "__interrupt__" in paused
    publisher.ci_state = "success"
    restarted = graph(
        manager,
        runtime,
        RecordingMemory(),
        delivery=publisher,
        build_runner=runner,
        checkpointer=saver,
    )
    finished = await restarted.ainvoke(Command(resume="retry"), config)
    assert finished["phase"] == WorkflowPhase.READY_FOR_HUMAN_REVIEW
    assert runner.calls == 1
    assert len(runtime.executor.tasks) == 1


def successful_build() -> BuildRunResult:
    return BuildRunResult(
        profile_name="python-tests",
        profile_digest=None,
        outcome=BuildOutcome.SUCCESS,
        phases=(
            BuildPhaseResult(
                phase="test",
                outcome=BuildOutcome.SUCCESS,
                command=("/usr/local/bin/python", "-m", "pytest"),
                image="python@sha256:" + "a" * 64,
                cwd=".",
                duration_seconds=0.1,
                exit_code=0,
            ),
        ),
    )


class FactoryRuntime:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.executor = Executor()
        self.registry = Registry(self.executor)
        self.judge = Judge()
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
        return self.judge


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
        build_profile=BuildProfileSelection(requested_profile="python-tests"),
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


def graph(
    manager,
    runtime,
    memory,
    *,
    build_runner=None,
    build_audit_recorder=None,
    delivery=None,
    checkpointer=None,
    architecture_policy=None,
    acceptance_suite=None,
):
    return build_workflow(
        Planner(),
        Registry(Executor()),
        Judge(),
        memory,
        checkpointer or MemorySaver(serde=build_serde()),
        workspace_manager=manager,
        runtime_factory=runtime,
        build_strategy_selector=BuildProfileRegistry(
            {
                "python-tests": BuildProfile(
                    name="python-tests",
                    ecosystem="python",
                    architecture=architecture_policy,
                    acceptance=acceptance_suite,
                    image="python@sha256:" + "a" * 64,
                    phases=(
                        BuildPhase(
                            name="test",
                            argv=("/usr/local/bin/python", "-m", "pytest"),
                        ),
                    ),
                )
            }
        ),
        build_runner=build_runner or BuildRunner(),
        build_audit_recorder=build_audit_recorder,
        delivery=delivery or Delivery(),
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

    assert output["phase"] == WorkflowPhase.READY_FOR_HUMAN_REVIEW
    assert output["workspace"].local_path == str(tmp_path)
    assert output["factory_stage"] == FactoryStage.READY_FOR_HUMAN_REVIEW
    assert output["build_strategy"].selected_profile == "python-tests"
    assert output["build_strategy"].selection_reason == "explicit"
    assert output["build_strategy"].profile_digest is not None
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


@pytest.mark.asyncio
async def test_unsupported_strategy_stops_before_grounding_or_planning(
    tmp_path: Path,
) -> None:
    workflow_id = "factory-unsupported"
    manager = WorkspaceManager(lease(tmp_path, workflow_id))
    runtime = FactoryRuntime(tmp_path)
    memory = RecordingMemory()
    app = build_workflow(
        Planner(),
        Registry(Executor()),
        Judge(),
        memory,
        MemorySaver(serde=build_serde()),
        workspace_manager=manager,
        runtime_factory=runtime,
        build_strategy_selector=BuildProfileRegistry({}),
    )
    config = {"configurable": {"thread_id": workflow_id}}

    output = await app.ainvoke(
        {
            "request": "corrigir o comportamento do widget",
            "project_id": "acme-widget",
            "workflow_id": workflow_id,
            "owner_client_id": "client",
            "work_order": work_order(),
        },
        config,
    )

    assert output["phase"] == WorkflowPhase.UNSUPPORTED_BUILD_STRATEGY
    assert output["build_strategy"].selection_reason == "unsupported"
    assert output["error"].startswith("unsupported_build_strategy:")
    assert output["__interrupt__"][0].value["reason"] == ("unsupported_build_strategy")
    assert output["__interrupt__"][0].value["options"] == ["retry", "abort"]
    assert runtime.leases == []
    assert memory.persisted == []


@pytest.mark.asyncio
async def test_strategy_selection_is_audited_before_project_grounding(
    tmp_path: Path,
) -> None:
    workflow_id = "factory-audited-strategy"
    active_lease = lease(tmp_path, workflow_id)
    events: list[BuildProfileSelection] = []

    async def record_strategy(**payload: object) -> None:
        events.append(payload["selection"])  # type: ignore[arg-type]

    app = build_workflow(
        Planner(),
        Registry(Executor()),
        Judge(),
        RecordingMemory(),
        MemorySaver(serde=build_serde()),
        workspace_manager=WorkspaceManager(active_lease),
        runtime_factory=FactoryRuntime(tmp_path),
        build_strategy_selector=BuildProfileRegistry(
            {
                "python-tests": BuildProfile(
                    name="python-tests",
                    ecosystem="python",
                    image="python@sha256:" + "a" * 64,
                    phases=(BuildPhase(name="test", argv=("/usr/local/bin/pytest",)),),
                )
            }
        ),
        strategy_audit_recorder=record_strategy,
        build_runner=BuildRunner(),
        delivery=Delivery(),
    )

    output = await app.ainvoke(
        {
            "request": "corrigir o comportamento do widget",
            "project_id": "acme-widget",
            "workflow_id": workflow_id,
            "owner_client_id": "client",
            "work_order": work_order(),
        },
        {"configurable": {"thread_id": workflow_id}},
    )

    assert output["phase"] == WorkflowPhase.READY_FOR_HUMAN_REVIEW
    assert len(events) == 1
    assert events[0].selected_profile == "python-tests"


@pytest.mark.asyncio
async def test_factory_build_evidence_reaches_attempt_judge_and_audit(
    tmp_path: Path,
) -> None:
    workflow_id = "factory-build-evidence"
    active_lease = lease(tmp_path, workflow_id)
    runtime = FactoryRuntime(tmp_path)
    runner = BuildRunner()
    audits: list[dict] = []

    async def record_build(**payload: object) -> None:
        audits.append(payload)  # type: ignore[arg-type]

    output = await graph(
        WorkspaceManager(active_lease),
        runtime,
        RecordingMemory(),
        build_runner=runner,
        build_audit_recorder=record_build,
    ).ainvoke(
        {
            "request": "corrigir o comportamento do widget",
            "project_id": "acme-widget",
            "workflow_id": workflow_id,
            "owner_client_id": "client",
            "work_order": work_order(),
        },
        {"configurable": {"thread_id": workflow_id}},
    )

    task = output["plan"][0]
    evidence = task.attempts[0].build_validation
    assert evidence is not None and evidence.outcome is BuildOutcome.SUCCESS
    assert task.attempts[0].factory_stage is FactoryStage.VALIDATION
    assert task.result["workspace"]["build_validation"]["outcome"] == "success"
    assert runtime.judge.contexts[0]["build_validation"]["outcome"] == "success"
    assert output["factory_stage"] is FactoryStage.READY_FOR_HUMAN_REVIEW
    assert runner.calls == 1
    assert audits[0]["report"].outcome is BuildOutcome.SUCCESS
    assert "## Validação em sandbox" in output["final_output"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_green_runner_cannot_omit_or_substitute_architecture_evidence(
    tmp_path, missing
):
    from app.models.architecture import ArchitecturePolicy, ArchitectureReport

    policy = ArchitecturePolicy(
        rules=[
            {
                "id": "domain",
                "source": "widget",
                "forbidden": ["requests"],
                "remediation": "Use uma interface de domínio em vez de HTTP direto.",
            }
        ]
    )
    report = successful_build().model_copy(
        update={
            "architecture": None
            if missing
            else ArchitectureReport(
                policy_digest="f" * 64,
                complete=True,
                files_checked=1,
            )
        }
    )
    publisher, runner = Delivery(), BuildRunner([report])
    workflow_id = "missing-architecture" if missing else "wrong-architecture"
    result = await graph(
        WorkspaceManager(lease(tmp_path, workflow_id)),
        FactoryRuntime(tmp_path),
        RecordingMemory(),
        build_runner=runner,
        delivery=publisher,
        architecture_policy=policy,
    ).ainvoke(
        {
            "request": "Validar limites do widget",
            "project_id": "p",
            "workflow_id": workflow_id,
            "owner_client_id": "c",
            "work_order": work_order(),
        },
        {"configurable": {"thread_id": workflow_id}},
    )
    assert not publisher.calls
    assert 0 < runner.calls <= 3
    assert all(not evaluation.approved for evaluation in result["evaluations"])
    assert (
        result["plan"][0].attempts[-1].build_validation.error_code
        == "architecture_evidence_missing_or_failed"
    )


@pytest.mark.asyncio
async def test_architecture_violation_reaches_retry_and_review(tmp_path):
    from app.factory.architecture import check_architecture
    from app.models.architecture import ArchitecturePolicy
    from app.agents.executor import LLMExecutor

    policy = ArchitecturePolicy(
        rules=[
            {
                "id": "domain",
                "source": "widget",
                "forbidden": ["requests"],
                "remediation": "Use uma interface de domínio e injete o cliente HTTP.",
            }
        ]
    )
    source = tmp_path / "widget.py"
    source.write_text("import requests")
    failed_evidence = check_architecture(tmp_path, policy)
    source.write_text("import os")
    passed_evidence = check_architecture(tmp_path, policy)
    bad = successful_build().model_copy(
        update={
            "outcome": BuildOutcome.POLICY_REJECTION,
            "error_code": "architecture_policy_failed",
            "architecture": failed_evidence,
        }
    )
    good = successful_build().model_copy(update={"architecture": passed_evidence})
    runner, runtime, publisher = (
        BuildRunner([bad, good]),
        FactoryRuntime(tmp_path),
        Delivery(),
    )
    output = await graph(
        WorkspaceManager(lease(tmp_path, "arch-retry")),
        runtime,
        RecordingMemory(),
        build_runner=runner,
        delivery=publisher,
        architecture_policy=policy,
    ).ainvoke(
        {
            "request": "Respeitar a arquitetura do widget",
            "project_id": "p",
            "workflow_id": "arch-retry",
            "owner_client_id": "c",
            "work_order": work_order(),
        },
        {"configurable": {"thread_id": "arch-retry"}},
    )
    assert runner.calls == 2 and len(publisher.calls) == 1
    assert output["evaluations"][0].approved is False
    assert "architecture" in output["evaluations"][0].validated_by
    feedback = runtime.executor.tasks[1].result["workspace"]["command_feedback"]
    assert any(
        "widget.py:1" in item.get("details", "")
        and "requests" in item.get("details", "")
        for item in feedback
    )
    assert any("interface de domínio" in item.get("stdout", "") for item in feedback)
    context = runtime.executor.contexts[0]
    assert (
        "widget não pode importar requests" in context["architecture_policy_guidance"]
    )
    prompt = LLMExecutor._build_user_content(
        None,
        runtime.executor.tasks[0],
        context,
        previous_feedback="",
        current_iteration_feedback="",
    )
    assert "widget não pode importar requests" in prompt
    assert "Arquitetura: aprovada" in output["final_output"]
    assert output["phase"] is WorkflowPhase.READY_FOR_HUMAN_REVIEW


@pytest.mark.asyncio
async def test_failed_phase_vetoes_judge_and_feeds_bounded_retry(
    tmp_path: Path,
) -> None:
    failed = BuildRunResult(
        profile_name="python-tests",
        profile_digest=None,
        outcome=BuildOutcome.COMMAND_FAILURE,
        phases=(
            BuildPhaseResult(
                phase="test",
                outcome=BuildOutcome.COMMAND_FAILURE,
                command=("/usr/local/bin/python", "-m", "pytest"),
                image="python@sha256:" + "a" * 64,
                cwd=".",
                duration_seconds=0.2,
                exit_code=1,
                stderr="assertion failed",
            ),
        ),
    )
    runner = BuildRunner([failed, successful_build()])
    workflow_id = "factory-build-retry"
    runtime = FactoryRuntime(tmp_path)
    output = await graph(
        WorkspaceManager(lease(tmp_path, workflow_id)),
        runtime,
        RecordingMemory(),
        build_runner=runner,
    ).ainvoke(
        {
            "request": "corrigir o comportamento do widget",
            "project_id": "acme-widget",
            "workflow_id": workflow_id,
            "owner_client_id": "client",
            "work_order": work_order(),
        },
        {"configurable": {"thread_id": workflow_id}},
    )

    task = output["plan"][0]
    assert runner.calls == 2
    assert task.attempt_count == 2
    assert task.attempts[0].build_validation.outcome is BuildOutcome.COMMAND_FAILURE
    assert task.attempts[1].build_validation.outcome is BuildOutcome.SUCCESS
    assert "fases obrigatórias" in task.description
    assert "fases obrigatórias" in runtime.executor.tasks[1].description
    assert (
        runtime.executor.tasks[1].result["workspace"]["command_feedback"][0]["stderr"]
        == "assertion failed"
    )
    rejected, approved = output["evaluations"]
    assert rejected.approved is False
    assert rejected.tests_passed is False
    assert "sandbox" in rejected.validated_by
    assert approved.approved is True
    assert output["phase"] is WorkflowPhase.READY_FOR_HUMAN_REVIEW


@pytest.mark.asyncio
@pytest.mark.parametrize("ci_state", ["pending", "none", "skipped", "error"])
async def test_factory_unverified_delivery_pauses_and_retries_without_reexecution(
    tmp_path: Path,
    ci_state: str,
) -> None:
    workflow_id = "factory-ci-gate"
    runtime = FactoryRuntime(tmp_path)
    memory = RecordingMemory()
    publisher = Delivery(ci_state)
    runner = BuildRunner()
    app = graph(
        WorkspaceManager(lease(tmp_path, workflow_id)),
        runtime,
        memory,
        delivery=publisher,
        build_runner=runner,
    )
    config = {"configurable": {"thread_id": workflow_id}}
    output = await app.ainvoke(
        {
            "request": "corrigir o comportamento do widget",
            "project_id": "p",
            "workflow_id": workflow_id,
            "owner_client_id": "c",
            "work_order": work_order(),
        },
        config,
    )
    gate = output["__interrupt__"][0].value
    assert gate["reason"] == f"factory_delivery_{ci_state}"
    assert gate["options"] == ["retry", "abort"]
    assert memory.persisted == []
    first = publisher.calls[0]["config"]
    assert first.head_branch == f"forgehand/{workflow_id}"
    assert first.pinned_base_sha == "a" * 40
    assert first.expected_head_sha is None
    publisher.ci_state = "success"
    resumed = await app.ainvoke(Command(resume="retry"), config)
    assert resumed["phase"] == WorkflowPhase.READY_FOR_HUMAN_REVIEW
    assert resumed["factory_stage"] == FactoryStage.READY_FOR_HUMAN_REVIEW
    assert memory.persisted[-1].phase == WorkflowPhase.READY_FOR_HUMAN_REVIEW
    assert publisher.calls[1]["config"].expected_head_sha == "c" * 40
    assert len(runtime.executor.tasks) == runner.calls == 1
    service = WorkflowService(
        app, Settings(), InMemoryWorkflowQueue(), run_workers=False
    )
    assert (await service.get(workflow_id))[
        "phase"
    ] == WorkflowPhase.READY_FOR_HUMAN_REVIEW
    with pytest.raises(WorkflowAlreadyTerminal):
        await service.cancel(workflow_id)


@pytest.mark.asyncio
async def test_factory_partial_decision_cannot_bypass_ci_gate(tmp_path: Path):
    workflow_id = "factory-no-partial"
    publisher = Delivery("pending")
    app = graph(
        WorkspaceManager(lease(tmp_path, workflow_id)),
        FactoryRuntime(tmp_path),
        RecordingMemory(),
        delivery=publisher,
    )
    config = {"configurable": {"thread_id": workflow_id}}
    await app.ainvoke(
        {
            "request": "corrigir o comportamento do widget",
            "project_id": "p",
            "workflow_id": workflow_id,
            "owner_client_id": "c",
            "work_order": work_order(),
        },
        config,
    )
    output = await app.ainvoke(Command(resume="accept_partial"), config)
    assert output["phase"] == WorkflowPhase.FAILED
    assert len(publisher.calls) == 1
