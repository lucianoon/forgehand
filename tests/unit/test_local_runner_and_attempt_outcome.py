"""Achados da segunda rodada real (03/09/2026).

1. `python -m pytest` disparado pelo servidor sob `uv run` no Windows caía no
   interpretador base (sem pytest): o runner local agora resolve argv[0] pelo
   PATH antes de criar o processo.
2. A tentativa julgada ficava com outcome=RUNNING para sempre, mesmo com a
   tarefa COMPLETED ou REJECTED.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.graph.contracts import NodeDependencies
from app.graph.phase_execution import build_execution_nodes
from app.graph.state import WorkflowState
from app.infrastructure import workspace_runtime
from app.infrastructure.workspace_runtime import resolve_argv
from app.models.task import (
    AgentTask,
    Capability,
    EvaluationResult,
    TaskAttempt,
    TaskStatus,
)


def test_resolve_argv_uses_path_lookup(monkeypatch) -> None:
    monkeypatch.setattr(
        workspace_runtime.shutil, "which", lambda name: f"/venv/bin/{name}"
    )
    assert resolve_argv(["python", "-m", "pytest", "-q"]) == [
        "/venv/bin/python",
        "-m",
        "pytest",
        "-q",
    ]


def test_resolve_argv_keeps_name_when_not_found(monkeypatch) -> None:
    monkeypatch.setattr(workspace_runtime.shutil, "which", lambda name: None)
    assert resolve_argv(["inexistente", "--x"]) == ["inexistente", "--x"]
    assert resolve_argv([]) == []


def _task_with_running_attempt() -> AgentTask:
    task = AgentTask(
        title="t",
        description="d",
        capability=Capability.BACKEND,
        acceptance_criteria=["ok"],
    )
    attempt = TaskAttempt(
        attempt_number=1,
        agent_name="backend",
        model="m",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        outcome=TaskStatus.RUNNING,
    )
    return task.model_copy(update={"status": TaskStatus.RUNNING, "attempts": [attempt]})


class _Judge:
    def __init__(self, approved: bool) -> None:
        self.approved = approved

    async def evaluate(self, task, context):
        return EvaluationResult(
            task_id=task.id,
            approved=self.approved,
            score=1.0 if self.approved else 0.2,
            criteria_scores={},
            failures=[] if self.approved else ["faltou o teste de divisão por zero"],
            required_changes=[],
        )


class _Memory:
    async def load_context(self, project_id, request=""):
        return {}

    async def persist(self, state):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_judged_attempt_outcome_matches_task_status(approved: bool) -> None:
    deps = NodeDependencies(
        planner=None, registry=None, judge=_Judge(approved), memory=_Memory()
    )
    nodes = build_execution_nodes(deps)
    task = _task_with_running_attempt()
    state = WorkflowState(
        workflow_id=str(uuid4()),
        project_id="p",
        owner_client_id="dev",
        request="r",
        plan=[task],
    )

    update = await nodes["evaluate_results"](state)

    judged = update["plan"][0]
    assert judged.status == (
        TaskStatus.COMPLETED if approved else judged.next_status_after_failure()
    )
    assert judged.attempts[-1].outcome == judged.status
    if approved:
        assert judged.attempts[-1].failure_reason is None
    else:
        assert "divisão por zero" in (judged.attempts[-1].failure_reason or "")
