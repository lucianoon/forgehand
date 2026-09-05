"""Compose installation probe using real API, queue, checkpoints, Git and Docker.

Run only as an explicit test entrypoint: python /probe/runtime.py api|worker.
The installed application has no test-mode setting or conditional behavior.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
from tempfile import NamedTemporaryFile
from typing import Any

import httpx
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

import app.api.container as composition
from app.factory.workspace import LocalGitWorkspaceManager
from app.graph.state import WorkflowPhase, WorkflowState
from app.models.build_execution import BuildOutcome
from app.models.factory import BuildProfileSelection


_ROOT = Path(os.environ["FORGEHAND_DATA_ROOT"]) / "probe"
_ROOT.mkdir(parents=True, exist_ok=True)


def _record(event: str, workflow_id: str, **details: Any) -> None:
    data = {"event": event, "workflow_id": workflow_id, "hostname": socket.gethostname(), **details}
    payload = (json.dumps(data, sort_keys=True) + "\n").encode()
    descriptor = os.open(_ROOT / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _blocked(workflow_id: str) -> None:
    with NamedTemporaryFile(mode="w", dir=_ROOT, prefix=".blocked-", delete=False) as temporary:
        json.dump({"workflow_id": workflow_id, "hostname": socket.gethostname()}, temporary)
        temporary.flush()
        os.fsync(temporary.fileno())
        path = Path(temporary.name)
    os.replace(path, _ROOT / "blocked.json")


class ProbeWorkspaceManager(LocalGitWorkspaceManager):
    def __init__(self, *args: Any, **kwargs: Any):
        kwargs.update(
            repository_url_resolver=lambda repository: str(_ROOT / "repository"),
            allow_local_repositories=True,
        )
        super().__init__(*args, **kwargs)


def deterministic_workflow(**components: Any) -> Any:
    manager = components["workspace_manager"]
    runner = components["build_runner"]
    registry = components["build_strategy_selector"]
    assert manager is not None and runner is not None and registry is not None
    profile = registry.get("installation-fixture")
    selection = BuildProfileSelection(
        requested_profile=profile.name,
        selected_profile=profile.name,
        selection_reason="explicit",
        phases=[phase.name.value for phase in profile.phases],
        profile_digest=profile.fingerprint(),
    )

    async def prepare(state: WorkflowState) -> dict[str, Any]:
        assert state.work_order is not None
        lease = await manager.provision(state.workflow_id, state.work_order)
        _record("prepare", state.workflow_id)
        return {
            "workspace": lease,
            "build_strategy": selection,
            "usage": {"tokens": 7, "cost_usd": 0.0},
            "phase": WorkflowPhase.EXECUTING,
        }

    async def work(state: WorkflowState) -> dict[str, Any]:
        import asyncio

        assert state.workspace is not None and state.build_strategy is not None
        if not (_ROOT / "release").exists():
            _blocked(state.workflow_id)
            _record("blocked", state.workflow_id)
        while not (_ROOT / "release").exists():
            await asyncio.sleep(0.1)
        evidence = await runner.run(state.workspace, state.build_strategy)
        if evidence.outcome != BuildOutcome.SUCCESS:
            _record("build_failure", state.workflow_id, outcome=evidence.outcome.value,
                    error_code=evidence.error_code,
                    phases=[{"outcome": phase.outcome.value, "error_code": phase.error_code,
                             "exit_code": phase.exit_code} for phase in evidence.phases])
            raise RuntimeError("Installation fixture Docker build failed")
        _record("build_success", state.workflow_id)
        return {
            "phase": WorkflowPhase.AWAITING_HUMAN,
            "context": {**state.context, "installation_probe_build": evidence.model_dump(mode="json")},
        }

    async def gate(state: WorkflowState) -> dict[str, Any]:
        decision = interrupt({"reason": "installation_approval", "options": ["retry", "abort"]})
        _record("approved", state.workflow_id, decision=decision)
        return {"human_decision": decision}

    async def finish(state: WorkflowState) -> dict[str, Any]:
        completed = state.human_decision == "retry"
        _record("finish", state.workflow_id, completed=completed)
        return {
            "phase": WorkflowPhase.COMPLETED if completed else WorkflowPhase.CANCELLED,
            "final_output": "Installation probe completed." if completed else "Installation probe aborted.",
        }

    builder = StateGraph(WorkflowState)
    for name, node in (("prepare", prepare), ("work", work), ("gate", gate), ("finish", finish)):
        builder.add_node(name, node)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "work")
    builder.add_edge("work", "gate")
    builder.add_edge("gate", "finish")
    builder.add_edge("finish", END)
    # The real checkpointer context supplies the production build_serde().
    return builder.compile(checkpointer=components["checkpointer"])


async def _forbid_outbound_http(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("The installation probe forbids outbound HTTP/LLM/SCM requests")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"api", "worker"}:
        raise SystemExit("usage: runtime.py api|worker")
    composition.LocalGitWorkspaceManager = ProbeWorkspaceManager
    composition.build_workflow = deterministic_workflow
    # Local Git and Docker socket operations remain real. No paid or external
    # HTTP call can silently enter this infrastructure experiment.
    httpx.AsyncClient.request = _forbid_outbound_http
    if sys.argv[1] == "api":
        import uvicorn
        from app.main import app

        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
    else:
        from app.worker import main as run_worker

        run_worker()


if __name__ == "__main__":
    main()
