"""ExecutionOutcome tipado e o escopo do timeout por tarefa no worker."""

from __future__ import annotations

import asyncio

import pytest

from app.graph.contracts import ExecutionOutcome, ExecutionPayload, NodeDependencies
from app.graph.phase_execution import build_execution_nodes
from app.models.task import AgentTask, Capability, EvaluationResult, TaskStatus


def test_from_executor_applies_historical_defaults_and_coercions() -> None:
    assert ExecutionOutcome.from_executor({}) == ExecutionOutcome()
    outcome = ExecutionOutcome.from_executor(
        {"result": {"summary": "ok"}, "agent": "backend", "model": "m",
         "tokens": "12", "cost_usd": 1, "budget_blocked_reason": ""}
    )
    assert outcome == ExecutionOutcome(
        result={"summary": "ok"}, agent="backend", model="m", tokens=12, cost_usd=1.0
    )
    typed = ExecutionOutcome(tokens=3)
    assert ExecutionOutcome.from_executor(typed) is typed


class _Judge:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay

    async def evaluate(self, task, context):
        await asyncio.sleep(self.delay)
        return EvaluationResult(
            task_id=task.id, approved=True, score=1.0, criteria_scores={},
            failures=[], required_changes=[],
        )


class _Executor:
    def __init__(self, outcome, delay: float = 0.0) -> None:
        self.outcome = outcome
        self.delay = delay

    async def execute(self, task, context):
        await asyncio.sleep(self.delay)
        return self.outcome


class _Registry:
    def __init__(self, executor) -> None:
        self.executor = executor

    def select(self, task):
        return self.executor


def _payload(timeout_seconds: float = 300) -> ExecutionPayload:
    task = AgentTask(
        title="t", description="d", capability=Capability.BACKEND,
        acceptance_criteria=["ok"],
    )
    # model_copy não revalida: permite um timeout curto sem esperar 1 s.
    task = task.model_copy(update={"timeout_seconds": timeout_seconds})
    return ExecutionPayload.model_construct(task=task, project_id="p", context={})


def _nodes(executor, judge=None):
    deps = NodeDependencies(
        planner=None, registry=_Registry(executor), judge=judge or _Judge(), memory=None
    )
    return build_execution_nodes(deps)


RAW = {"result": {"summary": "feito"}, "agent": "backend", "model": "m",
       "tokens": 10, "cost_usd": 0.01}


@pytest.mark.asyncio
@pytest.mark.parametrize("returned", [RAW, ExecutionOutcome.from_executor(RAW)])
async def test_worker_accepts_dict_or_typed_outcome(returned) -> None:
    update = await _nodes(_Executor(returned))["execute_task"](_payload())

    task = update["plan"][0]
    assert task.status == TaskStatus.COMPLETED
    assert task.result == {"summary": "feito"}
    attempt = task.attempts[-1]
    assert (attempt.agent_name, attempt.model, attempt.tokens_used) == ("backend", "m", 10)
    assert update["usage"] == {"tokens": 10, "cost_usd": 0.01}


@pytest.mark.asyncio
async def test_timeout_covers_executor_call() -> None:
    nodes = _nodes(_Executor(RAW, delay=5))
    payload = _payload(timeout_seconds=0.05)

    update = await nodes["execute_task"](payload)

    task = update["plan"][0]
    assert task.attempts[-1].outcome == TaskStatus.FAILED
    assert task.attempts[-1].failure_reason.startswith("timeout após")


@pytest.mark.asyncio
async def test_timeout_does_not_cover_judge() -> None:
    """Documentado no README: o relógio da tarefa cobre só o executor."""
    nodes = _nodes(_Executor(RAW), judge=_Judge(delay=0.2))
    payload = _payload(timeout_seconds=0.05)

    update = await nodes["execute_task"](payload)

    assert update["plan"][0].status == TaskStatus.COMPLETED
