"""Queue recovery must distinguish consecutive approvals inside one graph node."""

import asyncio
import copy
import os
from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.api.container import checkpointer_context
from app.api.service import WorkflowResumeUncertain, WorkflowService
from app.graph.state import WorkflowPhase
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import InMemoryWorkflowQueue, PostgresWorkflowQueue


class ApprovalState(TypedDict, total=False):
    workflow_id: str
    project_id: str
    owner_client_id: str
    request: str
    phase: str
    first_decision: str
    second_decision: str


@pytest.fixture(params=["memory", "postgres"])
async def approval_backend(request):
    if request.param == "memory":
        settings = Settings(_env_file=None, checkpointer_backend="memory")
        queue = InMemoryWorkflowQueue(lease_seconds=0.25, max_delivery_attempts=3)
        async with checkpointer_context(settings) as checkpointer:
            yield checkpointer, queue, settings
        await queue.close()
        return
    dsn = os.getenv("TEST_DATABASE_URL")
    if os.getenv("RUN_POSTGRES_TESTS") != "1" or not dsn:
        pytest.skip("requires explicitly configured test PostgreSQL")
    from psycopg import AsyncConnection, sql
    from psycopg.conninfo import make_conninfo

    schema = "same_node_" + uuid4().hex
    async with await AsyncConnection.connect(dsn, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped = make_conninfo(dsn, options=f"-c search_path={schema}")
        settings = Settings(
            _env_file=None, database_url=scoped, checkpointer_backend="postgres"
        )
        queue = PostgresWorkflowQueue(scoped, lease_seconds=0.25)
        try:
            await queue.setup()
            async with checkpointer_context(settings) as checkpointer:
                yield checkpointer, queue, settings
        finally:
            await queue.close()
            await admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


async def first_approval_job(identical_payloads, approval_backend):
    first_payload = {"reason": "review" if identical_payloads else "first_gate"}
    second_payload = {"reason": "review" if identical_payloads else "second_gate"}

    async def both_gates(state):
        first = interrupt(first_payload)
        second = interrupt(second_payload)
        return {
            "first_decision": first,
            "second_decision": second,
            "phase": WorkflowPhase.COMPLETED.value,
        }

    async def prepare(state):
        # Real workflows set phase before entering a gate. PostgreSQL retains
        # ACKed queue rows, so phase-less state would be shown as queue-only.
        return {"phase": WorkflowPhase.AWAITING_HUMAN.value}

    builder = StateGraph(ApprovalState)
    builder.add_node("prepare", prepare)
    builder.add_node("both_gates", both_gates)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "both_gates")
    builder.add_edge("both_gates", END)
    checkpointer, queue, settings = approval_backend
    graph = builder.compile(checkpointer=checkpointer)
    service = WorkflowService(graph, settings, queue, run_workers=False)
    workflow_id = await service.start("p", "Review both decisions", None, "owner")
    start = await queue.dequeue("starter", 0.01)
    assert start is not None
    await service._invoke_job(start)
    assert await queue.acknowledge(start)
    assert (await service.get(workflow_id))["pending_decision"] == first_payload
    await service.decide(workflow_id, "approve-first")
    approval = await queue.dequeue("original-worker", 0.01)
    assert approval is not None
    return graph, queue, service, approval, second_payload


async def redeliver(queue, original):
    # Leave the actual delivery unacknowledged, as a lost worker would.
    await asyncio.sleep(0.30)
    replacement = await queue.dequeue("replacement-worker", 0.01)
    assert replacement is not None
    assert replacement.id == original.id
    assert replacement.attempt_count == 2
    return replacement


async def approve_second_gate(graph, queue, service, workflow_id):
    await service.decide(workflow_id, "approve-second")
    second_job = await queue.dequeue("second-approver-worker", 0.01)
    assert second_job is not None
    await service._invoke_job(second_job)
    assert await queue.acknowledge(second_job)
    completed = await graph.aget_state({"configurable": {"thread_id": workflow_id}})
    assert completed.interrupts == ()
    assert completed.next == ()
    assert completed.values["first_decision"] == "approve-first"
    assert completed.values["second_decision"] == "approve-second"
    assert completed.values["phase"] == WorkflowPhase.COMPLETED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("identical_payloads", [False, True], ids=["different", "same"])
async def test_consumed_approval_redelivery_preserves_next_interrupt_in_same_node(
    identical_payloads, approval_backend,
):
    graph, queue, service, job, second_payload = await first_approval_job(
        identical_payloads, approval_backend
    )
    config = {"configurable": {"thread_id": job.workflow_id}}
    first = await graph.aget_state(config)
    await service._invoke_job(job)
    second = await graph.aget_state(config)
    assert second.interrupts[0].value == second_payload
    # LangGraph reuses the task's interrupt ID even for a different decision.
    assert first.interrupts[0].id == second.interrupts[0].id

    replacement = await redeliver(queue, job)
    await service._invoke_job(replacement)
    assert await queue.acknowledge(replacement)
    after = await graph.aget_state(config)
    assert after.interrupts == second.interrupts, "Old approval consumed the next gate"
    assert after.values == second.values
    assert (await service.get(job.workflow_id))["pending_decision"] == second_payload

    await approve_second_gate(graph, queue, service, job.workflow_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("identical_payloads", [False, True], ids=["different", "same"])
async def test_approval_lost_before_invocation_still_resumes_first_interrupt(
    identical_payloads, approval_backend,
):
    graph, queue, service, job, second_payload = await first_approval_job(
        identical_payloads, approval_backend
    )
    # The worker died after dequeue but before invoking or consuming approval.
    replacement = await redeliver(queue, job)
    await service._invoke_job(replacement)
    assert await queue.acknowledge(replacement)
    waiting = await graph.aget_state({"configurable": {"thread_id": job.workflow_id}})
    assert len(waiting.interrupts) == 1
    assert waiting.interrupts[0].value == second_payload
    assert "second_decision" not in waiting.values

    await approve_second_gate(graph, queue, service, job.workflow_id)


async def replace_queued_envelope(queue, original, payload):
    assert await queue.acknowledge(original)
    await queue.enqueue(
        workflow_id=original.workflow_id,
        project_id=original.project_id,
        owner_client_id=original.owner_client_id,
        kind="resume",
        payload=payload,
    )
    replacement = await queue.dequeue("envelope-worker", 0.01)
    assert replacement is not None
    return replacement


@pytest.mark.asyncio
@pytest.mark.parametrize("ordinal", [-1, True, "0", 1], ids=["negative", "bool", "text", "ahead"])
async def test_corrupt_resume_ordinal_preserves_checkpoint(approval_backend, ordinal):
    graph, queue, service, job, _ = await first_approval_job(True, approval_backend)
    config = {"configurable": {"thread_id": job.workflow_id}}
    before = await graph.aget_state(config)
    payload = copy.deepcopy(job.payload)
    for position in payload["interrupt_positions"].values():
        position["resume_count"] = ordinal
    corrupt = await replace_queued_envelope(queue, job, payload)

    with pytest.raises(WorkflowResumeUncertain):
        await service._invoke_job(corrupt)
    after = await graph.aget_state(config)
    assert after == before
    assert await queue.fail(corrupt, "resume_decision_unbound")

    # The malformed message does not prevent a new correctly bound decision.
    await service.decide(job.workflow_id, "approve-first")
    fresh = await queue.dequeue("fresh-approver", 0.01)
    assert fresh is not None
    await service._invoke_job(fresh)
    assert await queue.acknowledge(fresh)
    await approve_second_gate(graph, queue, service, job.workflow_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("identical_payloads", [False, True], ids=["different", "same"])
@pytest.mark.parametrize("consumed", [False, True], ids=["before-consumption", "after-consumption"])
async def test_id_only_envelope_replay_requires_fresh_decision(
    approval_backend, identical_payloads, consumed,
):
    graph, queue, service, job, _ = await first_approval_job(
        identical_payloads, approval_backend
    )
    payload = {
        "resume_version": 1,
        "decision": job.payload["decision"],
        "interrupt_ids": job.payload["interrupt_ids"],
    }
    legacy = await replace_queued_envelope(queue, job, payload)
    if consumed:
        await service._invoke_job(legacy)
    config = {"configurable": {"thread_id": job.workflow_id}}
    before = await graph.aget_state(config)
    replacement = await redeliver(queue, legacy)

    with pytest.raises(WorkflowResumeUncertain):
        await service._invoke_job(replacement)
    after = await graph.aget_state(config)
    assert after == before
    assert await queue.fail(replacement, "resume_decision_unbound")

    if not consumed:
        await service.decide(job.workflow_id, "approve-first")
        fresh = await queue.dequeue("fresh-approver", 0.01)
        assert fresh is not None
        await service._invoke_job(fresh)
        assert await queue.acknowledge(fresh)
    await approve_second_gate(graph, queue, service, job.workflow_id)
