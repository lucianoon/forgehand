"""Subprocess faults using the real service, queue and PostgreSQL checkpointer."""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from app.api.container import checkpointer_context
from app.api.service import WorkflowService
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import PostgresWorkflowQueue


class ProbeState(TypedDict, total=False):
    workflow_id: str
    project_id: str
    owner_client_id: str
    request: str
    phase: str
    approved: str


def settings(dsn):
    return Settings(
        _env_file=None,
        database_url=dsn,
        checkpointer_backend="postgres",
        workflow_queue_backend="postgres",
        workflow_queue_lease_seconds=1.0,
        workflow_queue_poll_interval_seconds=0.02,
        workflow_worker_concurrency=1,
    )


def record(root, event):
    with (root / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "pid": os.getpid()}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def graph(checkpointer, root, scenario, block):
    async def prepare(state):
        record(root, "prepare")
        return {"phase": "completed" if scenario == "phase_completed" else "executing"}

    async def work(state):
        if block == "work":
            record(root, "blocked_work")
            await asyncio.Event().wait()
        record(root, "work")
        return {"phase": "executing"}

    async def gate(state):
        record(root, "gate")
        answer = interrupt({"reason": "test_approval"})
        record(root, "approved")
        return {"approved": answer}

    async def between(state):
        if block == "between":
            record(root, "blocked_between")
            await asyncio.Event().wait()
        record(root, "between")
        return {"phase": "executing"}

    async def second_gate(state):
        answer = interrupt({"reason": "second_approval"})
        record(root, "approved_second")
        return {"approved": answer}

    async def finish(state):
        record(root, "finish")
        return {"phase": "completed"}

    builder = StateGraph(ProbeState)
    builder.add_node("prepare", prepare)
    builder.add_node("work", work)
    builder.add_node("finish", finish)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "work")
    if scenario in {"gate", "two_gates"}:
        builder.add_node("gate", gate)
        builder.add_edge("work", "gate")
        if scenario == "two_gates":
            builder.add_node("between", between)
            builder.add_node("second_gate", second_gate)
            builder.add_edge("gate", "between")
            builder.add_edge("between", "second_gate")
            builder.add_edge("second_gate", "finish")
        else:
            builder.add_edge("gate", "finish")
    else:
        builder.add_edge("work", "finish")
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer)


async def main():
    root, scenario, block = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    config = settings(os.environ["FORGEHAND_CRASH_DSN"])
    done = asyncio.Event()

    class Queue(PostgresWorkflowQueue):
        async def dequeue(self, worker_id, poll_interval_seconds):
            job = await super().dequeue(worker_id, poll_interval_seconds)
            if job is not None and block == "dequeue":
                record(root, "blocked_dequeue")
                await asyncio.Event().wait()
            return job

        async def acknowledge(self, job):
            if block == "ack":
                record(root, "blocked_ack")
                await asyncio.Event().wait()
            acknowledged = await super().acknowledge(job)
            if acknowledged:
                done.set()
            return acknowledged

    queue = Queue(config.database_url, lease_seconds=1.0)
    await queue.setup()
    async with checkpointer_context(config) as cp:
        service = WorkflowService(graph(cp, root, scenario, block), config, queue, True)
        service.start_workers()
        try:
            await asyncio.wait_for(done.wait(), 20)
        finally:
            await service.shutdown()
            await queue.close()


if __name__ == "__main__":
    asyncio.run(main())
