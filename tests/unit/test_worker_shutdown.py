"""Interrupted work must remain available to a replacement worker."""

import asyncio
from types import SimpleNamespace

import pytest

from app.api.service import WorkflowService
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import InMemoryWorkflowQueue


class BlockingGraph:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def aget_state(self, config):
        return SimpleNamespace(values={}, next=(), tasks=(), interrupts=())

    async def ainvoke(self, payload, config):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class RecordingQueue(InMemoryWorkflowQueue):
    """Observe the delivery without replacing queue ownership behavior."""

    def __init__(self):
        super().__init__(lease_seconds=0.1, max_delivery_attempts=2)
        self.deliveries = []

    async def dequeue(self, worker_id, poll_interval_seconds):
        delivery = await super().dequeue(worker_id, poll_interval_seconds)
        if delivery is not None:
            self.deliveries.append(delivery)
        return delivery


@pytest.mark.asyncio
async def test_shutdown_preserves_interrupted_job_for_replacement_worker():
    graph = BlockingGraph()
    queue = RecordingQueue()
    service = WorkflowService(
        graph,
        Settings(
            _env_file=None,
            workflow_worker_concurrency=1,
            workflow_queue_lease_seconds=0.1,
            workflow_queue_poll_interval_seconds=0.005,
        ),
        queue,
        run_workers=True,
    )
    try:
        workflow_id = await service.start("p", "Finish this work", None, "owner")
        await asyncio.wait_for(graph.started.wait(), timeout=2)
        original = queue.deliveries[0]
        await asyncio.wait_for(service.shutdown(), timeout=2)

        assert graph.cancelled.is_set()
        interrupted = await queue.get_state(workflow_id)
        assert interrupted is not None, "Shutdown must not acknowledge unfinished work"
        assert interrupted.status == "processing"
        assert (await queue.get_stats()).done == 0

        # No heartbeats remain after shutdown, so another worker can reclaim it.
        await asyncio.sleep(0.15)
        replacement = await queue.dequeue("replacement-worker", 0.01)
        assert replacement is not None
        assert replacement.id == original.id
        assert replacement.workflow_id == workflow_id
        assert replacement.attempt_count == 2
        assert replacement.payload == original.payload
        assert replacement.locked_by != original.locked_by
        assert await queue.acknowledge(original) is False
        assert await queue.heartbeat(original) is False
        assert await queue.acknowledge(replacement) is True
        assert (await queue.get_stats()).done == 1
    finally:
        await service.shutdown()
