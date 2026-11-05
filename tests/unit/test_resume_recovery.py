"""A recovered approval must never authorize a different human gate."""

import asyncio
from contextlib import suppress
from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.api.service import WorkflowService
from app.graph.state import WorkflowPhase
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import InMemoryWorkflowQueue


class ApprovalState(TypedDict, total=False):
    workflow_id: str
    project_id: str
    owner_client_id: str
    request: str
    phase: str
    first_decision: str
    second_decision: str


class TwoGates:
    def __init__(self, block_between=False):
        self.block_between = block_between
        self.between_started = asyncio.Event()
        self.first_approvals = 0
        self.second_approvals = 0

    async def first(self, state):
        decision = interrupt({"reason": "first_gate"})
        self.first_approvals += 1
        return {"first_decision": decision, "phase": WorkflowPhase.EXECUTING.value}

    async def between(self, state):
        self.between_started.set()
        if self.block_between:
            await asyncio.Event().wait()
        return {"phase": WorkflowPhase.AWAITING_HUMAN.value}

    async def second(self, state):
        decision = interrupt({"reason": "second_gate"})
        self.second_approvals += 1
        return {"second_decision": decision, "phase": WorkflowPhase.COMPLETED.value}

    def graph(self):
        builder = StateGraph(ApprovalState)
        builder.add_node("first", self.first)
        builder.add_node("between", self.between)
        builder.add_node("second", self.second)
        builder.add_edge(START, "first")
        builder.add_edge("first", "between")
        builder.add_edge("between", "second")
        builder.add_edge("second", END)
        return builder.compile(checkpointer=MemorySaver())


async def first_approval_job(block_between=False):
    gates = TwoGates(block_between)
    graph = gates.graph()
    queue = InMemoryWorkflowQueue(lease_seconds=0.05, max_delivery_attempts=3)
    service = WorkflowService(graph, Settings(_env_file=None), queue, run_workers=False)
    workflow_id = await service.start("p", "Review both gates", None, "owner")
    start = await queue.dequeue("starter", 0.01)
    assert start is not None
    await service._invoke_job(start)
    assert await queue.acknowledge(start)
    assert (await service.get(workflow_id))["pending_decision"] == {
        "reason": "first_gate"
    }
    await service.decide(workflow_id, "approve-first")
    job = await queue.dequeue("original-worker", 0.01)
    assert job is not None
    return gates, graph, queue, service, job


async def redeliver(queue, original):
    # _invoke_job deliberately leaves the delivery unacknowledged, as a lost
    # worker would. Reclaim through the real queue rather than fabricating a job.
    await asyncio.sleep(0.08)
    replacement = await queue.dequeue("replacement-worker", 0.01)
    assert replacement is not None
    assert replacement.id == original.id
    assert replacement.attempt_count == 2
    return replacement


@pytest.mark.asyncio
async def test_first_resume_approves_only_the_current_gate():
    gates, _, queue, service, job = await first_approval_job()
    await service._invoke_job(job)
    assert await queue.acknowledge(job)

    status = await service.get(job.workflow_id)
    assert status["pending_decision"] == {"reason": "second_gate"}
    assert gates.first_approvals == 1
    assert gates.second_approvals == 0


@pytest.mark.asyncio
async def test_resume_redelivery_continues_after_consumed_approval():
    gates, graph, queue, service, job = await first_approval_job(block_between=True)
    invocation = asyncio.create_task(service._invoke_job(job))
    try:
        await asyncio.wait_for(gates.between_started.wait(), timeout=2)
    finally:
        invocation.cancel()
        with suppress(asyncio.CancelledError):
            await invocation
    config = {"configurable": {"thread_id": job.workflow_id}}
    interrupted = await graph.aget_state(config)
    assert interrupted.next == ("between",)
    assert interrupted.values["first_decision"] == "approve-first"

    gates.block_between = False
    replacement = await redeliver(queue, job)
    await service._invoke_job(replacement)
    assert await queue.acknowledge(replacement)

    recovered = await graph.aget_state(config)
    assert recovered.values["first_decision"] == "approve-first"
    assert "second_decision" not in recovered.values
    assert (await service.get(job.workflow_id))["pending_decision"] == {
        "reason": "second_gate"
    }
    assert gates.first_approvals == 1
    assert gates.second_approvals == 0


@pytest.mark.asyncio
async def test_resume_redelivery_does_not_apply_old_approval_to_later_gate():
    gates, graph, queue, service, job = await first_approval_job()
    await service._invoke_job(job)
    config = {"configurable": {"thread_id": job.workflow_id}}
    before = await graph.aget_state(config)
    assert before.interrupts[0].value == {"reason": "second_gate"}
    second_interrupt_id = before.interrupts[0].id

    replacement = await redeliver(queue, job)
    await service._invoke_job(replacement)
    assert await queue.acknowledge(replacement)

    after = await graph.aget_state(config)
    assert "second_decision" not in after.values, "Old approval bypassed the next gate"
    assert after.interrupts[0].id == second_interrupt_id
    assert gates.first_approvals == 1
    assert gates.second_approvals == 0

    # The next gate still accepts its own explicit decision normally.
    await service.decide(job.workflow_id, "approve-second")
    second_job = await queue.dequeue("second-approver-worker", 0.01)
    assert second_job is not None
    await service._invoke_job(second_job)
    assert await queue.acknowledge(second_job)
    completed = await graph.aget_state(config)
    assert completed.next == ()
    assert completed.interrupts == ()
    assert completed.values["second_decision"] == "approve-second"
    assert gates.second_approvals == 1


async def wait_for_queue_status(queue, workflow_id, expected):
    async def wait():
        while True:
            state = await queue.get_state(workflow_id)
            if state is not None and state.status == expected:
                return state
            if expected == "done" and state is None:
                return None
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(wait(), timeout=2)


def background_service(graph, queue):
    return WorkflowService(
        graph,
        Settings(
            _env_file=None,
            workflow_worker_concurrency=1,
            workflow_queue_lease_seconds=0.05,
            workflow_queue_poll_interval_seconds=0.005,
        ),
        queue,
        run_workers=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("approval_already_consumed", [False, True])
async def test_legacy_resume_redelivery_preserves_gate_for_fresh_decision(
    approval_already_consumed,
):
    gates, graph, queue, service, bound_job = await first_approval_job()
    assert await queue.acknowledge(bound_job)
    await queue.enqueue(
        workflow_id=bound_job.workflow_id,
        project_id="p",
        owner_client_id="owner",
        kind="resume",
        payload="approve-first",
    )
    legacy_job = await queue.dequeue("lost-legacy-worker", 0.01)
    assert legacy_job is not None
    if approval_already_consumed:
        await service._invoke_job(legacy_job)
    config = {"configurable": {"thread_id": bound_job.workflow_id}}
    before = await graph.aget_state(config)
    assert before.interrupts

    worker = background_service(graph, queue)
    try:
        worker.start_workers()
        rejected = await wait_for_queue_status(queue, bound_job.workflow_id, "failed")
        assert rejected.error == "resume_decision_unbound"
        preserved = await graph.aget_state(config)
        assert preserved.config == before.config
        assert preserved.values == before.values
        assert preserved.interrupts == before.interrupts
        assert gates.second_approvals == 0
        assert gates.first_approvals == int(approval_already_consumed)
        assert await queue.acknowledge(legacy_job) is False
    finally:
        await worker.shutdown()

    fresh = background_service(graph, queue)
    try:
        await fresh.decide(bound_job.workflow_id, "fresh-approval")
        await wait_for_queue_status(queue, bound_job.workflow_id, "done")
        after = await graph.aget_state(config)
        if approval_already_consumed:
            assert after.values["second_decision"] == "fresh-approval"
            assert after.interrupts == ()
        else:
            assert after.values["first_decision"] == "fresh-approval"
            assert after.interrupts[0].value == {"reason": "second_gate"}
    finally:
        await fresh.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"decision": None},
        {"decision": ""},
        {"decision": 1},
        {"interrupt_ids": []},
        {"interrupt_ids": [""]},
        {"interrupt_ids": [1]},
        {"resume_version": 2},
        {"resume_version": True},
        {"resume_version": None},
        {"remove_decision": True},
    ],
)
async def test_malformed_resume_envelope_preserves_checkpoint(invalid_fields):
    _, graph, queue, _, bound_job = await first_approval_job()
    assert await queue.acknowledge(bound_job)
    config = {"configurable": {"thread_id": bound_job.workflow_id}}
    before = await graph.aget_state(config)
    payload = {
        "resume_version": 1,
        "decision": "approve-first",
        "interrupt_ids": [before.interrupts[0].id],
        **invalid_fields,
    }
    if payload.pop("remove_decision", False):
        payload.pop("decision")
    await queue.enqueue(
        workflow_id=bound_job.workflow_id,
        project_id="p",
        owner_client_id="owner",
        kind="resume",
        payload=payload,
    )
    worker = background_service(graph, queue)
    try:
        worker.start_workers()
        rejected = await wait_for_queue_status(queue, bound_job.workflow_id, "failed")
        assert rejected.error == "resume_decision_unbound"
        after = await graph.aget_state(config)
        assert after.config == before.config
        assert after.values == before.values
        assert after.interrupts == before.interrupts
    finally:
        await worker.shutdown()


@pytest.mark.asyncio
async def test_bound_resume_redelivery_after_completion_does_not_reexecute_nodes():
    gates, graph, queue, service, first_job = await first_approval_job()
    await service._invoke_job(first_job)
    assert await queue.acknowledge(first_job)
    await service.decide(first_job.workflow_id, "approve-second")
    second_job = await queue.dequeue("original-second-worker", 0.01)
    assert second_job is not None
    await service._invoke_job(second_job)
    config = {"configurable": {"thread_id": first_job.workflow_id}}
    before = await graph.aget_state(config)
    assert before.next == ()
    assert before.values["second_decision"] == "approve-second"

    replacement = await redeliver(queue, second_job)
    await service._invoke_job(replacement)
    assert await queue.acknowledge(replacement)
    assert await queue.acknowledge(second_job) is False
    after = await graph.aget_state(config)
    assert after.config == before.config
    assert after.values == before.values
    assert gates.first_approvals == 1
    assert gates.second_approvals == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_stage", ["between", "completed"])
async def test_legacy_resume_redelivery_without_pending_gate_recovers_safely(
    checkpoint_stage,
):
    gates, graph, queue, service, bound_job = await first_approval_job(
        block_between=checkpoint_stage == "between"
    )
    if checkpoint_stage == "completed":
        await service._invoke_job(bound_job)
    assert await queue.acknowledge(bound_job)
    await queue.enqueue(
        workflow_id=bound_job.workflow_id,
        project_id="p",
        owner_client_id="owner",
        kind="resume",
        payload="legacy-approval",
    )
    legacy_job = await queue.dequeue("lost-legacy-worker", 0.01)
    assert legacy_job is not None
    if checkpoint_stage == "between":
        invocation = asyncio.create_task(service._invoke_job(legacy_job))
        try:
            await asyncio.wait_for(gates.between_started.wait(), timeout=2)
        finally:
            invocation.cancel()
            with suppress(asyncio.CancelledError):
                await invocation
        gates.block_between = False
    else:
        await service._invoke_job(legacy_job)
    config = {"configurable": {"thread_id": bound_job.workflow_id}}
    before = await graph.aget_state(config)
    assert before.interrupts == ()
    assert before.next == (("between",) if checkpoint_stage == "between" else ())

    worker = background_service(graph, queue)
    try:
        worker.start_workers()
        await wait_for_queue_status(queue, bound_job.workflow_id, "done")
        after = await graph.aget_state(config)
        assert gates.first_approvals == 1
        if checkpoint_stage == "between":
            assert after.values["first_decision"] == "legacy-approval"
            assert after.interrupts[0].value == {"reason": "second_gate"}
            assert gates.second_approvals == 0
        else:
            assert after.config == before.config
            assert after.values == before.values
            assert gates.second_approvals == 1
    finally:
        await worker.shutdown()
