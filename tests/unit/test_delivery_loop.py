"""Ciclo até o PR verde: publish_delivery no grafo, CI vermelho reabre as
tarefas que publicaram e volta ao replan; esgotadas as iterações, gate humano;
parcial aceito nunca volta ao ciclo."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.graph.state import (
    DeliveryConfig,
    DeliveryResult,
    WorkflowBudget,
    WorkflowPhase,
    merge_tasks_by_id,
)
from app.graph.workflow import build_serde, build_workflow
from app.infrastructure.scm import GitHubDeliveryService
from app.models.task import AgentTask, Capability, EvaluationResult, TaskStatus


class Memory:
    async def load_context(self, project_id, request=""):
        return {}

    async def persist(self, state):
        self.persisted = state


class Planner:
    def __init__(self, tasks):
        self._tasks = tasks

    async def create_plan(self, request, context):
        return self._tasks


class Registry:
    def __init__(self, executor):
        self.executor = executor

    def select(self, task):
        return self.executor


class PublishingExecutor:
    """Executor cujo resultado publica um arquivo (workspace.published_files)."""

    def __init__(self):
        self.calls = 0
        self.descriptions: list[str] = []

    async def execute(self, task, context):
        self.calls += 1
        self.descriptions.append(task.description)
        return {
            "result": {
                "summary": f"tentativa {self.calls}",
                "workspace": {
                    "applied_files": ["app/svc.py"],
                    "published_files": [
                        {"path": "app/svc.py", "content": f"v{self.calls}\n"}
                    ],
                    "deleted_paths": [],
                },
            },
            "agent": "fake",
            "model": "fake",
            "tokens": 10,
            "cost_usd": 0.01,
        }


class AnalysisExecutor:
    async def execute(self, task, context):
        return {
            "result": {"summary": "só análise"},
            "agent": "fake",
            "model": "fake",
            "tokens": 1,
            "cost_usd": 0.0,
        }


class ApprovingJudge:
    async def evaluate(self, task, context):
        return EvaluationResult(
            task_id=task.id, approved=True, score=1, criteria_scores={"ok": 1}
        )


class ScriptedDelivery:
    """Cada publish devolve o próximo DeliveryResult do script."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[dict] = []

    async def publish(
        self, *, config, workflow_id, project_id, files, deletions, summary, details=""
    ):
        self.calls.append(
            {
                "config": config,
                "files": files,
                "deletions": deletions,
                "summary": summary,
            }
        )
        return self._results.pop(0)


def failure(*failures: str, sha: str) -> DeliveryResult:
    return DeliveryResult(
        pull_request_number=42,
        url="https://gh.test/pr/42",
        branch="forgehand/wf",
        commit_sha=sha,
        ci_state="failure",
        failures=list(failures),
        files=1,
    )


def success(sha: str) -> DeliveryResult:
    return DeliveryResult(
        pull_request_number=42,
        url="https://gh.test/pr/42",
        branch="forgehand/wf",
        commit_sha=sha,
        ci_state="success",
        files=1,
    )


def task() -> AgentTask:
    return AgentTask(
        title="backend",
        description="implementar",
        capability=Capability.BACKEND,
        acceptance_criteria=["endpoint responde"],
    )


def initial(workflow_id: str, *, delivery=True, max_iterations=3):
    data = {
        "request": "teste",
        "project_id": "p",
        "workflow_id": workflow_id,
        "owner_client_id": "client-p",
        "budget": WorkflowBudget(max_iterations=max_iterations),
    }
    if delivery:
        data["delivery"] = {"repository": "acme/service", "base_branch": "main"}
    return data


def build(executor, delivery, memory=None):
    return build_workflow(
        Planner([task()]),
        Registry(executor),
        ApprovingJudge(),
        memory or Memory(),
        MemorySaver(serde=build_serde()),
        delivery=delivery,
    )


@pytest.mark.asyncio
async def test_ci_failure_reopens_publishing_tasks_and_loops_until_green():
    executor = PublishingExecutor()
    delivery = ScriptedDelivery(
        [
            failure(
                "test: failure — 1 failed", "test: tests/test_x.py:3 assert", sha="c1"
            ),
            success("c2"),
        ]
    )
    app = build(executor, delivery)
    wid = str(uuid4())

    out = await app.ainvoke(initial(wid), {"configurable": {"thread_id": wid}})

    assert out["phase"] == WorkflowPhase.COMPLETED
    assert len(delivery.calls) == 2
    assert delivery.calls[0]["files"] == [{"path": "app/svc.py", "content": "v1\n"}]
    assert delivery.calls[1]["files"] == [{"path": "app/svc.py", "content": "v2\n"}]
    assert isinstance(delivery.calls[0]["config"], DeliveryConfig)

    result = out["delivery_result"]
    assert result.ci_state == "success" and result.attempts == 2
    assert out["iteration"] == 1, "um replan disparado pelo CI"

    (t,) = out["plan"]
    assert t.status == TaskStatus.COMPLETED
    assert t.attempt_count == 2
    assert t.reopen_reason == "ci:c1"
    assert executor.calls == 2
    assert "O CI do pull request reprovou" in executor.descriptions[1]
    assert "test: tests/test_x.py:3 assert" in executor.descriptions[1]

    ci_evaluations = [e for e in out["evaluations"] if e.validated_by == ["ci"]]
    assert len(ci_evaluations) == 1
    assert ci_evaluations[0].approved is False
    assert ci_evaluations[0].tests_passed is False
    assert ci_evaluations[0].failures[0].startswith("[ci] test: failure")

    assert "Pull request: https://gh.test/pr/42" in out["final_output"]
    assert "CI: success" in out["final_output"]


@pytest.mark.asyncio
async def test_ci_failure_with_iterations_exhausted_goes_to_human_gate():
    executor = PublishingExecutor()
    delivery = ScriptedDelivery(
        [
            failure("lint: failure", sha="c1"),
            failure("lint: failure", sha="c2"),
        ]
    )
    app = build(executor, delivery)
    wid = str(uuid4())
    cfg = {"configurable": {"thread_id": wid}}

    out = await app.ainvoke(initial(wid, max_iterations=1), cfg)

    # 1ª falha → replan (iteração 1) → 2ª falha com iterações esgotadas → gate
    snapshot = await app.aget_state(cfg)
    interrupts = [i.value for t in snapshot.tasks for i in (t.interrupts or ())]
    assert interrupts, "esperava interrupt do gate humano"
    assert interrupts[0]["reason"] == "ci_failed_iterations_exhausted"
    assert interrupts[0]["delivery"]["ci_state"] == "failure"
    assert interrupts[0]["delivery"]["failures"] == ["lint: failure"]
    assert len(delivery.calls) == 2
    assert out["plan"][0].status == TaskStatus.REJECTED

    # humano aceita parcial: a tarefa reaberta segue REJECTED, então não há
    # nada novo aprovado a publicar; a última publicação (CI vermelho) é
    # mantida como verdade sobre o PR e o ciclo NÃO recomeça.
    out = await app.ainvoke(Command(resume="accept_partial"), cfg)
    assert out["phase"] == WorkflowPhase.COMPLETED
    assert len(delivery.calls) == 2
    assert out["delivery_result"].ci_state == "failure"
    assert out["delivery_result"].attempts == 2
    assert out["delivery_result"].commit_sha == "c2"
    assert "última publicação mantida" in out["delivery_result"].note
    assert "CI: failure" in out["final_output"]
    assert "Entrega PARCIAL" in out["final_output"]


@pytest.mark.asyncio
async def test_delivery_skipped_when_nothing_publishable_and_absent_without_config():
    delivery = ScriptedDelivery([])
    wid = str(uuid4())
    out = await build(AnalysisExecutor(), delivery).ainvoke(
        initial(wid), {"configurable": {"thread_id": wid}}
    )
    assert out["phase"] == WorkflowPhase.COMPLETED
    assert delivery.calls == []
    assert out["delivery_result"].ci_state == "skipped"
    assert "nenhum arquivo publicável" in out["delivery_result"].note

    wid2 = str(uuid4())
    out2 = await build(PublishingExecutor(), delivery).ainvoke(
        initial(wid2, delivery=False), {"configurable": {"thread_id": wid2}}
    )
    assert out2["phase"] == WorkflowPhase.COMPLETED
    assert out2.get("delivery_result") is None
    assert delivery.calls == []


@pytest.mark.asyncio
async def test_delivery_error_does_not_reopen_tasks():
    delivery = ScriptedDelivery(
        [DeliveryResult(ci_state="error", error="SCMError: 403", files=1)]
    )
    wid = str(uuid4())
    out = await build(PublishingExecutor(), delivery).ainvoke(
        initial(wid), {"configurable": {"thread_id": wid}}
    )
    assert out["phase"] == WorkflowPhase.COMPLETED
    assert out["plan"][0].status == TaskStatus.COMPLETED
    assert out["delivery_result"].error == "SCMError: 403"
    assert "Erro: SCMError: 403" in out["final_output"]


def test_reducer_reopens_completed_task_only_with_new_reopen_reason():
    base = task()
    completed = base.model_copy(update={"status": TaskStatus.COMPLETED})
    later = datetime.now(timezone.utc) + timedelta(seconds=1)

    stale = base.model_copy(update={"status": TaskStatus.REJECTED, "updated_at": later})
    assert merge_tasks_by_id([completed], [stale])[0].status == TaskStatus.COMPLETED

    reopened = stale.model_copy(update={"reopen_reason": "ci:c1"})
    assert merge_tasks_by_id([completed], [reopened])[0].status == TaskStatus.REJECTED

    # mesmo motivo de novo (update tardio da mesma reabertura) não rebaixa
    done_again = reopened.model_copy(update={"status": TaskStatus.COMPLETED})
    same_reason = reopened.model_copy(
        update={"updated_at": later + timedelta(seconds=1)}
    )
    assert (
        merge_tasks_by_id([done_again], [same_reason])[0].status == TaskStatus.COMPLETED
    )


@pytest.mark.asyncio
async def test_github_delivery_service_reports_missing_credentials_as_error():
    service = GitHubDeliveryService(token_provider_factory=lambda: None)
    result = await service.publish(
        config=DeliveryConfig(repository="acme/service"),
        workflow_id="wf",
        project_id="p",
        files=[{"path": "a", "content": "x"}],
        deletions=[],
        summary="s",
    )
    assert result.ci_state == "error"
    assert "credencial" in (result.error or "")
