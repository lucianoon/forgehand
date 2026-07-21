import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agents.judge import LLMJudge
from app.api.service import WorkflowService
from app.graph.state import WorkflowBudget, WorkflowPhase, merge_tasks_by_id
from app.graph.workflow import build_serde, build_workflow
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import InMemoryWorkflowQueue, _serialize_payload
from app.models.task import (
    AgentTask,
    Capability,
    EvaluationResult,
    TaskBudget,
    TaskStatus,
)
from app.providers.base import CompletionResult, Usage


class Memory:
    async def load_context(self, project_id, request=""):
        return {}

    async def persist(self, state):
        return None


class Registry:
    def __init__(self, executor):
        self.executor = executor

    def select(self, task):
        return self.executor


class ApprovingJudge:
    async def evaluate(self, task, context):
        return EvaluationResult(
            task_id=task.id,
            approved=True,
            score=1,
            criteria_scores={"ok": 1},
        )


class RejectingJudge:
    async def evaluate(self, task, context):
        return EvaluationResult(
            task_id=task.id,
            approved=False,
            score=0,
            criteria_scores={"ok": 0},
            required_changes=["corrigir"],
        )


class Executor:
    def __init__(self, tokens=1, cost=0.0):
        self.tokens = tokens
        self.cost = cost
        self.calls = 0

    async def execute(self, task, context):
        self.calls += 1
        return {
            "result": {"ok": True},
            "agent": "fake",
            "model": "fake",
            "tokens": self.tokens,
            "cost_usd": self.cost,
        }


def initial(workflow_id, budget=None):
    data = {
        "request": "teste",
        "project_id": "p",
        "workflow_id": workflow_id,
        "owner_client_id": "client-p",
    }
    if budget is not None:
        data["budget"] = budget
    return data


class FakeGraphApp:
    def __init__(self, delay=0.05):
        self.delay = delay
        self.values_by_thread = {}

    async def ainvoke(self, initial, config):
        await asyncio.sleep(self.delay)
        thread_id = config["configurable"]["thread_id"]
        self.values_by_thread[thread_id] = {
            **initial,
            "phase": WorkflowPhase.LOADING_CONTEXT,
            "iteration": 0,
            "usage": {"tokens": 0, "cost_usd": 0.0},
        }
        return self.values_by_thread[thread_id]

    async def aget_state(self, config):
        thread_id = config["configurable"]["thread_id"]
        return SimpleNamespace(
            values=self.values_by_thread.get(thread_id),
            tasks=(),
            interrupts=(),
        )

    async def aupdate_state(self, config, values):
        thread_id = config["configurable"]["thread_id"]
        current = self.values_by_thread.get(thread_id, {})
        self.values_by_thread[thread_id] = {**current, **values}


def test_postgres_queue_payload_serializes_pydantic_budget():
    payload = _serialize_payload(
        {
            "workflow_id": "wf",
            "budget": WorkflowBudget(max_tokens=123, max_cost_usd=0.5),
        }
    )

    assert '"max_tokens": 123' in payload
    assert '"max_cost_usd": 0.5' in payload


@pytest.mark.asyncio
async def test_dependency_waves_do_not_consume_replan_iterations():
    class ChainPlanner:
        async def create_plan(self, request, context):
            tasks = []
            for index in range(5):
                tasks.append(
                    AgentTask(
                        title=f"t{index}",
                        description="ok",
                        capability=Capability.BACKEND,
                        acceptance_criteria=["ok"],
                        dependencies=[tasks[-1].id] if tasks else [],
                    )
                )
            return tasks

    app = build_workflow(
        ChainPlanner(),
        Registry(Executor()),
        ApprovingJudge(),
        Memory(),
        MemorySaver(serde=build_serde()),
    )
    output = await app.ainvoke(
        initial("chain", WorkflowBudget(max_iterations=1)),
        {"configurable": {"thread_id": "chain"}},
    )

    assert output["phase"].value == "completed"
    assert output["iteration"] == 0
    assert all(task.status == TaskStatus.COMPLETED for task in output["plan"])


def test_merge_preserves_escalated_except_explicit_retry():
    task = AgentTask(
        title="backend",
        description="ok",
        capability=Capability.BACKEND,
        acceptance_criteria=["ok"],
    )
    existing = [
        task.model_copy(
            update={
                "status": TaskStatus.ESCALATED,
                "updated_at": datetime.now(timezone.utc),
            }
        )
    ]
    later = datetime.now(timezone.utc) + timedelta(seconds=1)
    running_update = [
        task.model_copy(update={"status": TaskStatus.RUNNING, "updated_at": later})
    ]
    reopened = [
        task.model_copy(update={"status": TaskStatus.REJECTED, "updated_at": later})
    ]

    merged_running = merge_tasks_by_id(existing, running_update)
    merged_retry = merge_tasks_by_id(existing, reopened)

    assert merged_running[0].status == TaskStatus.ESCALATED
    assert merged_retry[0].status == TaskStatus.REJECTED


@pytest.mark.asyncio
async def test_partial_delivery_lists_pending_tasks():
    class DeadlockPlanner:
        async def create_plan(self, request, context):
            return [
                AgentTask(
                    title="bloqueada",
                    description="ok",
                    capability=Capability.BACKEND,
                    acceptance_criteria=["ok"],
                    dependencies=[uuid4()],
                )
            ]

    app = build_workflow(
        DeadlockPlanner(),
        Registry(Executor()),
        ApprovingJudge(),
        Memory(),
        MemorySaver(serde=build_serde()),
    )
    config = {"configurable": {"thread_id": "deadlock"}}
    interrupted = await app.ainvoke(initial("deadlock"), config)

    assert interrupted["__interrupt__"][0].value["reason"] == "dependency_deadlock"
    output = await app.ainvoke(Command(resume="accept_partial"), config)
    assert "PARCIAL" in output["final_output"]
    assert "bloqueada" in output["final_output"]
    assert "pending" in output["final_output"]


@pytest.mark.asyncio
async def test_zero_dispatch_capacity_goes_to_human_gate():
    class Planner:
        async def create_plan(self, request, context):
            return [
                AgentTask(
                    title="backend",
                    description="ok",
                    capability=Capability.BACKEND,
                    acceptance_criteria=["ok"],
                )
            ]

    class ZeroCapacityRegistry(Registry):
        def dispatch_policy(self, task):
            return "backend", 0

    app = build_workflow(
        Planner(),
        ZeroCapacityRegistry(Executor()),
        ApprovingJudge(),
        Memory(),
        MemorySaver(serde=build_serde()),
    )
    config = {"configurable": {"thread_id": "zero-capacity"}}
    interrupted = await app.ainvoke(initial("zero-capacity"), config)

    assert interrupted["__interrupt__"][0].value["reason"] == "dependency_deadlock"


@pytest.mark.asyncio
async def test_task_budget_overrun_escalates_and_retry_grants_headroom():
    class BudgetPlanner:
        async def create_plan(self, request, context):
            return [
                AgentTask(
                    title="cara",
                    description="ok",
                    capability=Capability.BACKEND,
                    acceptance_criteria=["ok"],
                    max_attempts=1,
                    budget=TaskBudget(max_tokens=10, max_cost_usd=0.001),
                )
            ]

    executor = Executor(tokens=100, cost=0.1)
    app = build_workflow(
        BudgetPlanner(),
        Registry(executor),
        ApprovingJudge(),
        Memory(),
        MemorySaver(serde=build_serde()),
    )
    config = {"configurable": {"thread_id": "task-budget"}}
    interrupted = await app.ainvoke(initial("task-budget"), config)

    assert interrupted["plan"][0].status == TaskStatus.ESCALATED
    assert interrupted["plan"][0].budget.exceeded
    assert executor.calls == 1

    output = await app.ainvoke(Command(resume="retry"), config)
    assert output["phase"].value == "completed"
    assert executor.calls == 2


@pytest.mark.asyncio
async def test_global_budget_retry_executes_another_attempt():
    class Planner:
        async def create_plan(self, request, context):
            return [
                AgentTask(
                    title="cara",
                    description="ok",
                    capability=Capability.BACKEND,
                    acceptance_criteria=["ok"],
                )
            ]

    executor = Executor(tokens=100, cost=0.1)
    app = build_workflow(
        Planner(),
        Registry(executor),
        RejectingJudge(),
        Memory(),
        MemorySaver(serde=build_serde()),
    )
    config = {"configurable": {"thread_id": "global-budget"}}
    interrupted = await app.ainvoke(
        initial("global-budget", WorkflowBudget(max_cost_usd=0.05)), config
    )
    assert interrupted["__interrupt__"][0].value["reason"] == "budget_exhausted"
    assert executor.calls == 1

    retried = await app.ainvoke(Command(resume="retry"), config)
    assert executor.calls == 2
    assert retried["__interrupt__"][0].value["reason"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_registry_parallel_limit_is_enforced():
    class ParallelPlanner:
        async def create_plan(self, request, context):
            return [
                AgentTask(
                    title=f"t{index}",
                    description="ok",
                    capability=Capability.BACKEND,
                    acceptance_criteria=["ok"],
                )
                for index in range(7)
            ]

    class LimitedRegistry(Registry):
        def dispatch_policy(self, task):
            return "backend", 2

    class TrackingExecutor(Executor):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0

        async def execute(self, task, context):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return await super().execute(task, context)

    executor = TrackingExecutor()
    app = build_workflow(
        ParallelPlanner(),
        LimitedRegistry(executor),
        ApprovingJudge(),
        Memory(),
        MemorySaver(serde=build_serde()),
    )
    output = await app.ainvoke(
        initial("parallel"), {"configurable": {"thread_id": "parallel"}}
    )

    assert output["phase"].value == "completed"
    assert executor.max_active == 2
    assert output["iteration"] == 0


@pytest.mark.asyncio
async def test_background_failure_becomes_failed_state():
    class FailingPlanner:
        async def create_plan(self, request, context):
            raise RuntimeError("falha simulada")

    app = build_workflow(
        FailingPlanner(),
        Registry(Executor()),
        ApprovingJudge(),
        Memory(),
        MemorySaver(serde=build_serde()),
    )
    queue = InMemoryWorkflowQueue()
    service = WorkflowService(app, Settings(), queue, run_workers=True)
    workflow_id = await service.start(
        "p", "requisição suficientemente longa", None, "client-p"
    )

    for _ in range(100):
        await asyncio.sleep(0.01)
        status = await service.get(workflow_id)
        if status["phase"].value == "failed":
            break

    assert status["phase"].value == "failed"
    assert status["error"].startswith("RuntimeError")

    recovered_service = WorkflowService(app, Settings(), queue, run_workers=False)
    recovered = await recovered_service.get(workflow_id)
    assert recovered["error"].startswith("RuntimeError")

    await service.shutdown()
    await recovered_service.shutdown()


@pytest.mark.asyncio
async def test_judge_failure_escalates_task_instead_of_leaving_it_running():
    class SingleTaskPlanner:
        async def create_plan(self, request, context):
            return [
                AgentTask(
                    title="review",
                    description="analisar",
                    capability=Capability.REVIEW,
                    acceptance_criteria=["resumo correto"],
                )
            ]

    class FailingJudge:
        async def evaluate(self, task, context):
            raise RuntimeError("structured output inválido")

    app = build_workflow(
        SingleTaskPlanner(),
        Registry(Executor()),
        FailingJudge(),
        Memory(),
        MemorySaver(serde=build_serde()),
    )
    output = await app.ainvoke(
        initial("judge-failure"),
        {"configurable": {"thread_id": "judge-failure"}},
    )

    assert "__interrupt__" in output
    task = output["plan"][0]
    assert task.status == TaskStatus.ESCALATED
    assert task.attempts[-1].outcome == TaskStatus.FAILED
    assert "judge RuntimeError" in (task.attempts[-1].failure_reason or "")


@pytest.mark.asyncio
async def test_start_enqueues_and_exposes_queued_state_before_worker_finishes():
    app = FakeGraphApp(delay=0.05)
    queue = InMemoryWorkflowQueue()
    service = WorkflowService(
        app,
        Settings(workflow_start_timeout_seconds=1.0),
        queue,
        run_workers=True,
    )

    started = time.perf_counter()
    workflow_id = await service.start(
        "p", "requisição suficientemente longa", None, "client-p"
    )
    elapsed = time.perf_counter() - started
    queued = await service.get(workflow_id)

    assert elapsed < 0.04
    assert queued["phase"] == WorkflowPhase.QUEUED
    assert queued["workflow_id"] == workflow_id

    await asyncio.sleep(0.06)
    snapshot = await app.aget_state({"configurable": {"thread_id": workflow_id}})

    assert snapshot.values is not None
    assert snapshot.values["workflow_id"] == workflow_id
    await service.shutdown()


@pytest.mark.asyncio
async def test_cancel_stops_running_invocation_and_persists_terminal_state():
    app = FakeGraphApp(delay=10)
    queue = InMemoryWorkflowQueue()
    service = WorkflowService(
        app,
        Settings(workflow_queue_poll_interval_seconds=0.005),
        queue,
        run_workers=True,
    )
    workflow_id = await service.start(
        "p", "requisição suficientemente longa", None, "client-p"
    )

    for _ in range(100):
        state = await queue.get_state(workflow_id)
        if state is not None and state.status == "processing":
            break
        await asyncio.sleep(0.005)

    await service.cancel(workflow_id)
    await asyncio.sleep(0.01)
    cancelled = await service.get(workflow_id)

    assert cancelled["phase"] == WorkflowPhase.CANCELLED
    assert cancelled["error"] == "WorkflowCancelled"
    assert workflow_id not in app.values_by_thread
    await service.shutdown()


@pytest.mark.asyncio
async def test_cancel_removes_queued_and_processing_jobs():
    queue = InMemoryWorkflowQueue()
    await queue.enqueue(
        workflow_id="wf-queued",
        project_id="p",
        owner_client_id="client-p",
        kind="start",
        payload={"workflow_id": "wf-queued"},
    )
    assert await queue.cancel("wf-queued") is True
    assert await queue.dequeue("worker", 0.001) is None
    queued_state = await queue.get_state("wf-queued")
    assert queued_state is not None
    assert queued_state.error == "WorkflowCancelled"

    await queue.enqueue(
        workflow_id="wf-running",
        project_id="p",
        owner_client_id="client-p",
        kind="start",
        payload={"workflow_id": "wf-running"},
    )
    assert await queue.dequeue("worker", 0.001) is not None
    assert await queue.cancel("wf-running") is True
    running_state = await queue.get_state("wf-running")
    assert running_state is not None
    assert running_state.error == "WorkflowCancelled"


@pytest.mark.asyncio
async def test_api_and_worker_can_be_separated_with_shared_queue():
    app = FakeGraphApp(delay=0.01)
    queue = InMemoryWorkflowQueue()
    api_service = WorkflowService(
        app,
        Settings(),
        queue,
        run_workers=False,
    )
    worker_service = WorkflowService(
        app,
        Settings(),
        queue,
        run_workers=True,
    )
    worker_service.start_workers()

    workflow_id = await api_service.start(
        "p", "requisição suficientemente longa", None, "client-p"
    )
    queued = await api_service.get(workflow_id)
    assert queued["phase"] == WorkflowPhase.QUEUED

    await asyncio.sleep(0.03)
    snapshot = await app.aget_state({"configurable": {"thread_id": workflow_id}})
    assert snapshot.values is not None
    assert snapshot.values["workflow_id"] == workflow_id

    await api_service.shutdown()
    await worker_service.shutdown()


@pytest.mark.asyncio
async def test_in_memory_queue_requeues_expired_job_before_failing():
    queue = InMemoryWorkflowQueue(lease_seconds=0.01, max_delivery_attempts=2)
    await queue.enqueue(
        workflow_id="wf-lease",
        project_id="p",
        owner_client_id="client-p",
        kind="start",
        payload={"workflow_id": "wf-lease"},
    )

    first_delivery = await queue.dequeue("worker-a", 0.01)
    assert first_delivery is not None
    assert first_delivery.attempt_count == 1

    await asyncio.sleep(0.02)

    second_delivery = await queue.dequeue("worker-b", 0.01)
    assert second_delivery is not None
    assert second_delivery.workflow_id == "wf-lease"
    assert second_delivery.attempt_count == 2

    await queue.acknowledge(second_delivery)
    assert await queue.get_state("wf-lease") is None


@pytest.mark.asyncio
async def test_in_memory_queue_heartbeat_keeps_lease_and_enforces_owner():
    queue = InMemoryWorkflowQueue(lease_seconds=0.03, max_delivery_attempts=2)
    await queue.enqueue(
        workflow_id="wf-heartbeat",
        project_id="p",
        owner_client_id="client-p",
        kind="start",
        payload={"workflow_id": "wf-heartbeat"},
    )
    delivery = await queue.dequeue("worker-a", 0.01)
    assert delivery is not None

    await asyncio.sleep(0.02)
    assert await queue.heartbeat(delivery) is True
    await asyncio.sleep(0.02)
    assert await queue.dequeue("worker-b", 0.005) is None

    stale = delivery.__class__(**{**delivery.__dict__, "locked_by": "worker-b"})
    assert await queue.acknowledge(stale) is False
    assert await queue.acknowledge(delivery) is True


@pytest.mark.asyncio
async def test_worker_renews_lease_for_job_longer_than_lease():
    app = FakeGraphApp(delay=0.12)
    queue = InMemoryWorkflowQueue(lease_seconds=0.03, max_delivery_attempts=2)
    settings = Settings(
        workflow_queue_lease_seconds=0.03,
        workflow_queue_poll_interval_seconds=0.005,
        workflow_worker_concurrency=2,
    )
    service = WorkflowService(app, settings, queue, run_workers=True)

    workflow_id = await service.start(
        "p", "requisição suficientemente longa", None, "client-p"
    )
    await asyncio.sleep(0.16)
    stats = await queue.get_stats()

    assert workflow_id in app.values_by_thread
    assert stats.delivered_total == 1
    assert stats.requeued_total == 0
    assert stats.done == 1
    await service.shutdown()


@pytest.mark.asyncio
async def test_readiness_requires_expected_embedded_workers():
    service = WorkflowService(
        FakeGraphApp(),
        Settings(workflow_worker_concurrency=2),
        InMemoryWorkflowQueue(),
        True,
    )

    before = await service.readiness()
    assert before["ready"] is False
    assert before["embedded_workers_running"] == 0
    assert before["embedded_workers_expected"] == 2

    service.start_workers()
    after = await service.readiness()
    assert after["ready"] is True
    assert after["embedded_workers_running"] == 2
    await service.shutdown()


@pytest.mark.asyncio
async def test_readiness_requires_registered_external_postgres_worker():
    queue = InMemoryWorkflowQueue()
    settings = Settings(
        checkpointer_backend="postgres",
        workflow_queue_backend="postgres",
        run_embedded_workflow_workers=False,
    )
    service = WorkflowService(FakeGraphApp(), settings, queue, run_workers=False)

    before = await service.readiness()
    await queue.touch_worker("external-worker-1")
    after = await service.readiness()

    assert before["ready"] is False
    assert before["external_worker_health"] is False
    assert after["ready"] is True
    assert after["registered_workers"] == 1
    assert (await service.metrics())["workers"]["registered"] == 1
    await service.shutdown()


@pytest.mark.asyncio
async def test_in_memory_queue_marks_job_failed_after_expired_lease_limit():
    queue = InMemoryWorkflowQueue(lease_seconds=0.01, max_delivery_attempts=1)
    await queue.enqueue(
        workflow_id="wf-dead",
        project_id="p",
        owner_client_id="client-p",
        kind="start",
        payload={"workflow_id": "wf-dead"},
    )

    first_delivery = await queue.dequeue("worker-a", 0.01)
    assert first_delivery is not None
    assert first_delivery.attempt_count == 1

    await asyncio.sleep(0.02)

    expired = await queue.dequeue("worker-b", 0.01)
    assert expired is None

    state = await queue.get_state("wf-dead")
    assert state is not None
    assert state.status == "failed"
    assert state.error == "LeaseExpired"


@pytest.mark.asyncio
async def test_route_to_execution_passes_task_scoped_evidence():
    class GroundedMemory(Memory):
        async def load_context(self, project_id, request=""):
            return {
                "repository_grounding": {
                    "repo_root": "/repo",
                    "require_citations": True,
                    "evidence": [
                        {
                            "id": "E1",
                            "path": "app/main.py",
                            "line_start": 1,
                            "line_end": 3,
                            "excerpt": "from fastapi import FastAPI",
                        }
                    ],
                }
            }

    class Planner:
        async def create_plan(self, request, context):
            return [
                AgentTask(
                    title="analisar",
                    description="inspecionar arquitetura",
                    capability=Capability.ARCHITECTURE,
                    acceptance_criteria=["usa evidencias do repo"],
                    evidence_ids=["E1"],
                )
            ]

    class CapturingExecutor(Executor):
        def __init__(self):
            super().__init__()
            self.seen_contexts = []

        async def execute(self, task, context):
            self.seen_contexts.append(context)
            return {
                "result": {"ok": True, "citations": ["E1"]},
                "agent": "fake",
                "model": "fake",
                "tokens": self.tokens,
                "cost_usd": self.cost,
            }

    executor = CapturingExecutor()
    app = build_workflow(
        Planner(),
        Registry(executor),
        ApprovingJudge(),
        GroundedMemory(),
        MemorySaver(serde=build_serde()),
    )

    out = await app.ainvoke(
        initial("grounded-scope"),
        {"configurable": {"thread_id": "grounded-scope"}},
    )

    assert out["phase"].value == "completed"
    assert executor.seen_contexts[0]["task_evidence_ids"] == ["E1"]
    assert (
        executor.seen_contexts[0]["repository_grounding"]["evidence"][0]["id"] == "E1"
    )


@pytest.mark.asyncio
async def test_grounded_judge_rejects_missing_citations_even_if_llm_approves():
    class StaticRouter:
        async def complete(self, tier, request):
            return CompletionResult(
                text="ok",
                parsed={
                    "criteria": [
                        {
                            "criterion": "cita evidencias",
                            "score": 1.0,
                            "reasoning": "ok",
                        }
                    ],
                    "failures": [],
                    "required_changes": [],
                    "overall_score": 1.0,
                    "approved": True,
                },
                model="fake",
                provider="fake",
                usage=Usage(),
                cost_usd=0.0,
                latency_ms=0.0,
            )

    judge = LLMJudge(StaticRouter())
    task = AgentTask(
        title="analisar",
        description="mapear repo",
        capability=Capability.RESEARCH,
        acceptance_criteria=["cita evidencias"],
        evidence_ids=["E1"],
        result={"summary": "repo usa fastapi"},
    )
    outcome = await judge.evaluate(
        task,
        {
            "repository_grounding": {
                "repo_root": "/repo",
                "require_citations": True,
                "evidence": [
                    {
                        "id": "E1",
                        "path": "app/main.py",
                        "line_start": 1,
                        "line_end": 3,
                        "excerpt": "from fastapi import FastAPI",
                    }
                ],
            }
        },
    )

    assert outcome.evaluation.approved is False
    assert outcome.evaluation.validated_by == ["grounding"]
    assert any("citations" in failure for failure in outcome.evaluation.failures)


@pytest.mark.asyncio
async def test_grounded_judge_ignores_spurious_citation_feedback_when_citations_are_valid():
    class StaticRouter:
        async def complete(self, tier, request):
            return CompletionResult(
                text="ok",
                parsed={
                    "criteria": [
                        {
                            "criterion": "cita evidencias",
                            "score": 1.0,
                            "reasoning": "ok",
                        }
                    ],
                    "failures": [
                        "Ausência de citations válidas com evidence_ids reais do contexto do repositório."
                    ],
                    "required_changes": [
                        "Incluir 'citations' com evidence_ids reais do contexto do repositório."
                    ],
                    "overall_score": 1.0,
                    "approved": False,
                },
                model="fake",
                provider="fake",
                usage=Usage(),
                cost_usd=0.0,
                latency_ms=0.0,
            )

    judge = LLMJudge(StaticRouter())
    task = AgentTask(
        title="analisar",
        description="mapear repo",
        capability=Capability.RESEARCH,
        acceptance_criteria=["cita evidencias"],
        evidence_ids=["E1"],
        result={"summary": "repo usa fastapi", "citations": ["E1"]},
    )
    outcome = await judge.evaluate(
        task,
        {
            "repository_grounding": {
                "repo_root": "/repo",
                "require_citations": True,
                "evidence": [
                    {
                        "id": "E1",
                        "path": "app/main.py",
                        "line_start": 1,
                        "line_end": 3,
                        "excerpt": "from fastapi import FastAPI",
                    }
                ],
            }
        },
    )

    assert outcome.evaluation.approved is True
    assert outcome.evaluation.failures == []
    assert outcome.evaluation.required_changes == []


@pytest.mark.asyncio
async def test_grounded_judge_ignores_portuguese_citation_validity_rejection():
    class StaticRouter:
        async def complete(self, tier, request):
            return CompletionResult(
                text="ok",
                parsed={
                    "criteria": [
                        {
                            "criterion": "Citações válidas e grounding do repositório",
                            "score": 0.0,
                            "reasoning": "IDs supostamente inválidos",
                        }
                    ],
                    "failures": [
                        "As citações E1 não têm evidências válidas no repositório."
                    ],
                    "required_changes": [
                        "Incluir evidências que comprovem a validade das citações."
                    ],
                    "overall_score": 0.4,
                    "approved": False,
                },
                model="fake",
                provider="fake",
                usage=Usage(),
                cost_usd=0.0,
                latency_ms=0.0,
            )

    judge = LLMJudge(StaticRouter())
    task = AgentTask(
        title="analisar",
        description="mapear repo",
        capability=Capability.RESEARCH,
        acceptance_criteria=["Citações válidas e grounding do repositório"],
        evidence_ids=["E1"],
        result={"summary": "repo usa fastapi [E1]", "citations": ["E1"]},
    )
    outcome = await judge.evaluate(
        task,
        {
            "repository_grounding": {
                "repo_root": "/repo",
                "require_citations": True,
                "evidence": [
                    {
                        "id": "E1",
                        "path": "app/main.py",
                        "line_start": 1,
                        "line_end": 3,
                        "excerpt": "from fastapi import FastAPI",
                    }
                ],
            }
        },
    )

    assert outcome.evaluation.approved is True
    assert outcome.evaluation.criteria_scores == {
        "Citações válidas e grounding do repositório": 1.0
    }
    assert outcome.evaluation.failures == []
    assert outcome.evaluation.required_changes == []


@pytest.mark.asyncio
async def test_judge_overrides_spurious_minimal_change_rejection_with_workspace_diff():
    class StaticRouter:
        async def complete(self, tier, request):
            return CompletionResult(
                text="ok",
                parsed={
                    "criteria": [
                        {
                            "criterion": "arquivo existe",
                            "score": 1.0,
                            "reasoning": "ok",
                        },
                        {
                            "criterion": "a alteração é mínima e restrita ao arquivo novo",
                            "score": 0.0,
                            "reasoning": "arquivo foi modificado após criação",
                        },
                    ],
                    "failures": [
                        "O arquivo foi modificado após a criação, o que não atende ao critério de alteração mínima."
                    ],
                    "required_changes": [
                        "Recrie o arquivo sem modificações após sua criação."
                    ],
                    "overall_score": 0.5,
                    "approved": False,
                },
                model="fake",
                provider="fake",
                usage=Usage(),
                cost_usd=0.0,
                latency_ms=0.0,
            )

    judge = LLMJudge(StaticRouter())
    task = AgentTask(
        title="smoke",
        description="criar arquivo",
        capability=Capability.BACKEND,
        acceptance_criteria=[
            "arquivo existe",
            "a alteração é mínima e restrita ao arquivo novo",
        ],
        result={
            "summary": "arquivo criado",
            "workspace": {
                "file_diffs": [
                    {
                        "path": "generated/runtime_smoke_marker.py",
                        "change_type": "created",
                        "changed": True,
                        "diff": "new file",
                    }
                ]
            },
        },
    )

    outcome = await judge.evaluate(task, {})

    assert outcome.evaluation.approved is True
    assert (
        outcome.evaluation.criteria_scores[
            "a alteração é mínima e restrita ao arquivo novo"
        ]
        == 1.0
    )
    assert outcome.evaluation.failures == []
    assert outcome.evaluation.required_changes == []


@pytest.mark.asyncio
async def test_execute_task_persists_operational_summary_in_attempt():
    class Planner:
        async def create_plan(self, request, context):
            return [
                AgentTask(
                    title="backend",
                    description="aplicar diff",
                    capability=Capability.BACKEND,
                    acceptance_criteria=["arquivo gerado"],
                )
            ]

    class OperationalExecutor(Executor):
        async def execute(self, task, context):
            return {
                "result": {
                    "summary": "ok",
                    "files": [
                        {"path": "generated/example.py", "content": "print('ok')\n"}
                    ],
                    "notes": [],
                    "citations": [],
                    "workspace": {
                        "applied_files": ["generated/example.py"],
                        "file_diffs": [
                            {
                                "path": "generated/example.py",
                                "change_type": "created",
                                "changed": True,
                                "diff": "--- a/generated/example.py\n+++ b/generated/example.py",
                            }
                        ],
                        "command_feedback": [
                            {
                                "name": "ruff",
                                "passed": True,
                                "details": "lint ok",
                                "command": "uv run ruff check .",
                                "exit_code": 0,
                                "stdout": "lint ok",
                                "stderr": "",
                            }
                        ],
                        "operation_history": [
                            {
                                "step": "apply_file",
                                "path": "generated/example.py",
                                "change_type": "created",
                                "changed": True,
                            },
                            {
                                "step": "command_feedback",
                                "name": "ruff",
                                "passed": True,
                                "command": "uv run ruff check .",
                                "exit_code": 0,
                                "details": "lint ok",
                            },
                            {
                                "step": "git_snapshot",
                                "is_git_repo": True,
                                "status": " M generated/example.py",
                            },
                        ],
                        "git_snapshot": {
                            "status": " M generated/example.py",
                            "status_exit_code": 0,
                            "diff": "--- a/generated/example.py\n+++ b/generated/example.py",
                            "diff_exit_code": 0,
                        },
                        "strategy": {
                            "apply_files": True,
                            "run_objective_validation": True,
                            "allow_autocorrect": True,
                        },
                        "autocorrect": {
                            "enabled": True,
                            "max_rounds": 1,
                            "iterations": [
                                {
                                    "iteration": 1,
                                    "failed_checks": ["ruff"],
                                    "applied_files": ["generated/example.py"],
                                }
                            ],
                            "stopped_reason": "checks_passed_or_skipped",
                            "total_iterations": 1,
                        },
                    },
                },
                "agent": "fake",
                "model": "fake",
                "tokens": 10,
                "cost_usd": 0.1,
            }

    app = build_workflow(
        Planner(),
        Registry(OperationalExecutor()),
        ApprovingJudge(),
        Memory(),
        MemorySaver(serde=build_serde()),
    )

    out = await app.ainvoke(
        initial("operational-attempt"),
        {"configurable": {"thread_id": "operational-attempt"}},
    )

    attempt = out["plan"][0].attempts[0]
    assert attempt.operational_summary is not None
    assert attempt.operational_summary["applied_files"] == ["generated/example.py"]
    assert attempt.operational_summary["diff_paths"] == ["generated/example.py"]
    assert attempt.operational_summary["executed_commands"] == ["uv run ruff check ."]
    assert "generated/example.py" in attempt.operational_summary["git_status"]
    assert attempt.operational_summary["operation_steps"] == 3
    assert (
        "ruff: passed (exit_code=0)"
        in attempt.operational_summary["validation_feedback_text"]
    )
    assert attempt.operational_summary["strategy"]["run_objective_validation"] is True
    assert attempt.operational_summary["autocorrect"]["total_iterations"] == 1


@pytest.mark.asyncio
async def test_create_plan_replay_reuses_existing_plan_without_calling_planner():
    existing_task = AgentTask(
        title="existente",
        description="já planejada",
        capability=Capability.BACKEND,
        acceptance_criteria=["ok"],
    )

    class ReplayPlanner:
        def __init__(self):
            self.calls = 0

        async def create_plan(self, request, context):
            self.calls += 1
            return [
                AgentTask(
                    title="nova",
                    description="não deveria aparecer",
                    capability=Capability.BACKEND,
                    acceptance_criteria=["ok"],
                )
            ]

    planner = ReplayPlanner()
    app = build_workflow(
        planner,
        Registry(Executor()),
        ApprovingJudge(),
        Memory(),
        MemorySaver(serde=build_serde()),
    )

    out = await app.ainvoke(
        {
            **initial("replay-plan"),
            "plan": [existing_task],
        },
        {"configurable": {"thread_id": "replay-plan"}},
    )

    assert planner.calls == 0
    assert out["phase"].value == "completed"
    assert len(out["plan"]) == 1
    assert out["plan"][0].id == existing_task.id
