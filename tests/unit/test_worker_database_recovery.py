"""A database interruption must not permanently terminate queue consumers."""

import asyncio
from types import SimpleNamespace

import pytest

from app.api.service import WorkflowService
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import InMemoryWorkflowQueue

psycopg = pytest.importorskip("psycopg")


class CompletedGraph:
    def __init__(self, interrupted=None):
        self.interrupted = interrupted
        self.failed_once = False
        self.completed = False
        self.calls = 0

    async def aget_state(self, config):
        return SimpleNamespace(
            values={"phase": "completed"} if self.completed else {},
            next=(), tasks=(), interrupts=(),
        )

    async def ainvoke(self, payload, config):
        if self.interrupted == "checkpoint" and not self.failed_once:
            self.failed_once = True
            raise psycopg.OperationalError("test checkpoint database unavailable")
        self.calls += 1
        self.completed = True
        if self.interrupted == "heartbeat" and self.calls == 1:
            # The state is already durable; a later heartbeat interruption must
            # cancel the current invocation and recover from that checkpoint.
            await asyncio.Event().wait()


class InterruptedQueue(InMemoryWorkflowQueue):
    def __init__(self, interrupted):
        super().__init__(lease_seconds=0.02, max_delivery_attempts=3)
        self.interrupted = interrupted
        self.failed_once = False

    def interrupt(self, method):
        if method == self.interrupted and not self.failed_once:
            self.failed_once = True
            raise psycopg.OperationalError("test database unavailable")

    async def touch_worker(self, worker_id):
        self.interrupt("touch_worker")
        return await super().touch_worker(worker_id)

    async def dequeue(self, worker_id, poll_interval_seconds):
        self.interrupt("dequeue")
        return await super().dequeue(worker_id, poll_interval_seconds)

    async def acknowledge(self, job):
        self.interrupt("acknowledge")
        result = await super().acknowledge(job)
        self.interrupt("acknowledge_after_commit")
        return result

    async def heartbeat(self, job):
        self.interrupt("heartbeat")
        return await super().heartbeat(job)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", ["touch_worker", "dequeue", "acknowledge", "acknowledge_after_commit",
                                      "heartbeat", "checkpoint"])
async def test_database_interruption_recovers_consumer_without_replaying_completed_graph(interrupted):
    queue = InterruptedQueue(interrupted)
    graph = CompletedGraph(interrupted)
    service = WorkflowService(
        graph,
        Settings(_env_file=None, workflow_worker_concurrency=1,
                 workflow_queue_lease_seconds=0.02,
                 workflow_queue_poll_interval_seconds=0.005),
        queue, run_workers=True,
    )
    try:
        await service.start("p", "Complete once", None, "owner")
        async with asyncio.timeout(2):
            while (await queue.get_stats()).done != 1:
                await asyncio.sleep(0.01)
                if service._workers[0].done():
                    pytest.fail("Database interruption killed the queue consumer")
        assert queue.failed_once or graph.failed_once
        assert graph.calls == 1
        assert not service._workers[0].done()
        assert (await queue.get_stats()).failed == 0
    finally:
        # Retrieve a failed task during the red phase without hiding its assertion.
        for task in service._workers:
            task.cancel()
        await asyncio.gather(*service._workers, return_exceptions=True)
