"""Deterministic graph for the opt-in queue/database resilience drill.

Only this explicit entrypoint replaces graph construction. API authentication,
admission, PostgreSQL queue, leases, checkpoints and external workers stay real.
The synthetic usage below is a checkpoint assertion, never model billing.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
from typing import Any

import httpx
from langgraph.graph import END, START, StateGraph

import app.api.container as composition
from app.graph.state import WorkflowPhase, WorkflowState


ROOT = Path(os.environ["FORGEHAND_DATA_ROOT"]) / "probe"
ROOT.mkdir(parents=True, exist_ok=True)


def record(event: str, state: WorkflowState) -> None:
    payload = (json.dumps({
        "event": event, "workflow_id": state.workflow_id,
        "hostname": socket.gethostname(), "owner_client_id": state.owner_client_id,
        "project_id": state.project_id, "budget": state.budget.model_dump(),
        "usage": state.usage,
    }, sort_keys=True) + "\n").encode()
    descriptor = os.open(ROOT / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        assert os.write(descriptor, payload) == len(payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def deterministic_workflow(**components: Any) -> Any:
    async def prepare(state: WorkflowState) -> dict[str, Any]:
        record("prepare", state)
        return {
            "usage": {"tokens": state.budget.max_tokens - 93, "cost_usd": 0.0},
            "phase": WorkflowPhase.EXECUTING,
        }

    async def hold(state: WorkflowState) -> dict[str, Any]:
        record("holding", state)
        while not (ROOT / "release").exists():
            await asyncio.sleep(0.1)
        return {"phase": WorkflowPhase.PERSISTING}

    async def finish(state: WorkflowState) -> dict[str, Any]:
        # Append-only effects expose duplicate execution; never deduplicate here.
        record("effect", state)
        return {
            "phase": WorkflowPhase.COMPLETED,
            "final_output": f"Completed fixture for {state.owner_client_id}/{state.project_id}.",
        }

    builder = StateGraph(WorkflowState)
    for name, node in (("prepare", prepare), ("hold", hold), ("finish", finish)):
        builder.add_node(name, node)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "hold")
    builder.add_edge("hold", "finish")
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=components["checkpointer"])


def forbid_http(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("The resilience fixture forbids outbound HTTP/LLM/SCM calls")


async def forbid_async_http(*args: Any, **kwargs: Any) -> Any:
    forbid_http()


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"api", "worker"}:
        raise SystemExit("usage: runtime.py api|worker")
    composition.build_workflow = deterministic_workflow
    # Both normal requests and direct send() calls are denied before transport.
    httpx.Client.request = forbid_http
    httpx.Client.send = forbid_http
    httpx.AsyncClient.request = forbid_async_http
    httpx.AsyncClient.send = forbid_async_http
    if sys.argv[1] == "api":
        import uvicorn
        from app.main import app

        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
    else:
        from app.worker import main as run_worker

        run_worker()


if __name__ == "__main__":
    main()
