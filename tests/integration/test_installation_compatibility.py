"""PostgreSQL admission/lease enforcement, including consumers using legacy SQL."""

import asyncio
import os
from uuid import uuid4

import pytest

from app.infrastructure.workflow_queue import PostgresWorkflowQueue

pytestmark = pytest.mark.skipif(os.getenv("RUN_POSTGRES_TESTS") != "1", reason="requires test PostgreSQL")


@pytest.fixture
async def queues():
    from psycopg import AsyncConnection, sql
    from psycopg.conninfo import make_conninfo

    dsn = os.getenv("TEST_DATABASE_URL", "postgresql://forge:forge@localhost:5432/forgehand")
    schema = "installation_" + uuid4().hex
    async with await AsyncConnection.connect(dsn, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped = make_conninfo(dsn, options=f"-c search_path={schema}")
        result = [PostgresWorkflowQueue(scoped, lease_seconds=0.03) for _ in range(3)]
        try:
            for queue in result:
                await queue.setup()
            yield result
        finally:
            for queue in result:
                await queue.close()
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


async def admit(queue, workflow="workflow", *, factory=False):
    payload = {"request": "test"}
    if factory:
        payload["work_order"] = {"repository": {"full_name": "fixture/repo"}}
    return await queue.enqueue_start(workflow_id=workflow, project_id="p", owner_client_id="owner", payload=payload)


@pytest.mark.asyncio
async def test_incompatible_and_legacy_workers_cannot_claim_protected_job(queues):
    from psycopg.errors import RaiseException

    api, incompatible, legacy = queues
    api.configure_installation("deployment-a", required=True)
    incompatible.configure_installation("deployment-b", required=True)
    await admit(api)
    await api.touch_worker("good")
    await incompatible.touch_worker("wrong")
    await legacy.touch_worker("old")
    assert await incompatible.dequeue("wrong", 0) is None
    assert await legacy.dequeue("old", 0) is None
    for worker in ("wrong", "old"):
        with pytest.raises(RaiseException, match="worker_installation_incompatible"):
            async with legacy._conn.transaction():
                await legacy._conn.execute("""
                    UPDATE workflow_jobs SET status='processing', attempts=attempts+1,
                        locked_by=%s, locked_at=NOW() WHERE workflow_id='workflow'
                """, (worker,))
    job = await api.dequeue("good", 0)
    assert job and job.attempt_count == 1
    workers = await api.installation_workers("deployment-a")
    assert workers == {"active": 3, "compatible": 1, "incompatible": 2, "legacy": 1}


@pytest.mark.asyncio
async def test_crash_redelivery_preserves_installation_and_lease_ownership(queues):
    api, replacement, wrong = queues
    api.configure_installation("deployment-a", required=True)
    replacement.configure_installation("deployment-a", required=True)
    wrong.configure_installation("deployment-b", required=True)
    await admit(api)
    await api.touch_worker("first")
    original = await api.dequeue("first", 0)
    await asyncio.sleep(0.06)
    await wrong.touch_worker("wrong")
    assert await wrong.dequeue("wrong", 0) is None
    await replacement.touch_worker("replacement")
    recovered = await replacement.dequeue("replacement", 0)
    assert recovered and recovered.attempt_count == 2
    assert not await api.acknowledge(original)
    assert await replacement.acknowledge(recovered)


@pytest.mark.asyncio
async def test_resume_inherits_original_revision_instead_of_current_api(queues):
    original, updated, _ = queues
    original.configure_installation("deployment-a", required=True)
    updated.configure_installation("deployment-b", required=True)
    await admit(original)
    await original.touch_worker("old-compatible")
    start = await original.dequeue("old-compatible", 0)
    await original.acknowledge(start)
    await updated.enqueue("workflow", "p", "owner", "resume", "retry")
    await updated.touch_worker("new-incompatible")
    assert await updated.dequeue("new-incompatible", 0) is None
    resumed = await original.dequeue("old-compatible", 0)
    assert resumed and resumed.kind == "resume"
    assert (await updated.installation_jobs("deployment-b"))["incompatible"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [False, True])
async def test_factory_activation_preserves_but_blocks_unbound_legacy_work(queues, factory):
    old, new, _ = queues
    await admit(old, "existing-workflow", factory=factory)
    new.configure_installation("deployment-a", required=True)
    await new.touch_worker("current")
    await old.touch_worker("legacy")
    await admit(old, "old-api-after-activation", factory=factory)
    assert await old.dequeue("legacy", 0) is None
    assert await new.dequeue("current", 0) is None
    assert await new.installation_jobs("deployment-a") == {"incompatible": 2, "legacy_unbound": 2, "unconfigured": 0}
    assert (await old.get_state("existing-workflow")).status == "queued"


@pytest.mark.asyncio
async def test_factory_off_keeps_legacy_admission_and_heartbeat(queues):
    queue, _, _ = queues
    await admit(queue, factory=True)
    await queue.touch_worker("legacy")
    job = await queue.dequeue("legacy", 0)
    assert job and job.workflow_id == "workflow"
    assert await queue.acknowledge(job)


@pytest.mark.asyncio
async def test_missing_revision_does_not_enable_unconfigured_consumer(queues):
    api, worker, _ = queues
    api.configure_installation(None, required=True)
    worker.configure_installation(None, required=True)
    await admit(api)
    await worker.touch_worker("unknown")
    assert await worker.dequeue("unknown", 0) is None
    assert await api.installation_jobs(None) == {"incompatible": 1, "legacy_unbound": 0, "unconfigured": 1}


@pytest.mark.asyncio
async def test_activation_waits_for_inflight_legacy_admission(queues):
    old, new, observer = queues
    new.configure_installation("deployment-a", required=True)
    task = None
    try:
        async with old._conn.transaction():
            await old._conn.execute("""
                INSERT INTO workflow_jobs
                    (workflow_id, project_id, owner_client_id, kind, payload, status)
                VALUES ('racing-legacy', 'p', 'owner', 'start', '{}'::jsonb, 'queued')
            """)
            task = asyncio.create_task(new.touch_worker("current"))
            for _ in range(100):
                cursor = await observer._conn.execute(
                    "SELECT pg_blocking_pids(%s)", (new._conn.info.backend_pid,)
                )
                blockers = (await cursor.fetchone())[0]
                await observer._conn.commit()
                if old._conn.info.backend_pid in blockers:
                    break
                assert not task.done(), "Activation passed an uncommitted legacy insertion"
                await asyncio.sleep(0.01)
            else:
                pytest.fail("Activation did not acquire its policy lock")
        await task
        assert await new.installation_jobs("deployment-a") == {
            "incompatible": 1, "legacy_unbound": 1, "unconfigured": 0,
        }
        assert await new.dequeue("current", 0) is None
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
