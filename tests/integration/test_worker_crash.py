"""SIGKILL real workers; retain PostgreSQL checkpoints and recover leases."""

import asyncio
import json
import os
import signal
import sys
from uuid import uuid4

import pytest
from app.api.container import checkpointer_context
from app.infrastructure.workflow_queue import PostgresWorkflowQueue
from app.infrastructure.workspace_runtime import sanitized_environment
from tests.fixtures.crash_worker import graph, settings

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_TESTS") != "1", reason="requires test PostgreSQL"
    ),
    pytest.mark.skipif(os.name != "posix", reason="SIGKILL requires POSIX"),
]


@pytest.fixture
async def durable_queue():
    from psycopg import AsyncConnection, sql
    from psycopg.conninfo import make_conninfo

    dsn = os.getenv(
        "TEST_DATABASE_URL", "postgresql://forge:forge@localhost:5432/forgehand"
    )
    schema = "crash_test_" + uuid4().hex
    async with await AsyncConnection.connect(dsn, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped = make_conninfo(dsn, options=f"-c search_path={schema}")
        queue = PostgresWorkflowQueue(scoped, lease_seconds=1.0)
        await queue.setup()
        try:
            yield queue, scoped
        finally:
            await queue.close()
            await admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def events(root):
    path = root / "events.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


async def wait_for_event(root, event, child):
    async with asyncio.timeout(10):
        while event not in [item["event"] for item in events(root)]:
            if child.returncode is not None:
                raise AssertionError(
                    f"worker exited before {event}: {(await child.communicate())[1].decode()}"
                )
            await asyncio.sleep(0.02)


async def spawn_worker(root, dsn, scenario, block):
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.fixtures.crash_worker",
        str(root),
        scenario,
        block,
        env={
            **sanitized_environment(),
            "FORGEHAND_CRASH_DSN": dsn,
            "FORGEHAND_ENV_FILE": "",
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


@pytest.mark.parametrize(
    "scenario,block",
    [
        ("normal", "dequeue"),
        ("normal", "work"),
        ("normal", "ack"),
        ("gate", "ack"),
        ("phase_completed", "work"),
    ],
)
async def test_sigkill_recovers_checkpoint_without_replaying_completed_nodes(
    durable_queue, tmp_path, scenario, block
):
    queue, dsn = durable_queue
    wid = str(uuid4())
    job = await queue.enqueue(
        workflow_id=wid,
        project_id="p",
        owner_client_id="owner",
        kind="start",
        payload={
            "workflow_id": wid,
            "project_id": "p",
            "owner_client_id": "owner",
            "request": "test recovery",
        },
    )
    children = []
    try:
        first = await spawn_worker(tmp_path, dsn, scenario, block)
        children.append(first)
        await wait_for_event(tmp_path, "blocked_" + block, first)
        async with checkpointer_context(settings(dsn)) as cp:
            app = graph(cp, tmp_path, scenario, "none")
            config = {"configurable": {"thread_id": wid}}
            async with asyncio.timeout(5):
                before = await app.aget_state(config)
                while (block != "dequeue" and not before.values) or (
                    block == "work" and before.next != ("work",)
                ):
                    await asyncio.sleep(0.02)
                    before = await app.aget_state(config)
            first.kill()
            await first.communicate()
            assert first.returncode == -signal.SIGKILL
            second = await spawn_worker(tmp_path, dsn, scenario, "none")
            children.append(second)
            _, stderr = await asyncio.wait_for(second.communicate(), 15)
            assert second.returncode == 0, stderr.decode()
            assert first.pid != second.pid
            after = await app.aget_state(config)
        names = [item["event"] for item in events(tmp_path)]
        assert names.count("prepare") == 1, names
        assert names.count("work") == 1, names
        assert names.count("finish") == (0 if scenario == "gate" else 1), names
        if scenario == "gate":
            assert names.count("gate") == 1
            assert names.count("approved") == 0
            assert after.interrupts == before.interrupts
        else:
            assert not after.next
            assert after.values["phase"] == "completed"
        async with queue._lock:
            row = await (
                await queue._conn.execute(
                    "SELECT status, attempts, locked_by FROM workflow_jobs WHERE id=%s",
                    (int(job.id),),
                )
            ).fetchone()
        assert row == ("done", 2, None)
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
            await child.communicate()


@pytest.mark.parametrize("block", ["dequeue", "between", "ack"])
async def test_sigkill_resume_never_reuses_approval_for_later_gate(
    durable_queue, tmp_path, block
):
    from app.api.service import WorkflowService

    queue, dsn = durable_queue
    children = []
    async with checkpointer_context(settings(dsn)) as cp:
        app = graph(cp, tmp_path, "two_gates", "none")
        service = WorkflowService(app, settings(dsn), queue, False)
        wid = await service.start("p", "Approve both gates separately", None, "owner")
        start = await queue.dequeue("starter", 0.01)
        await service._invoke_job(start)
        assert await queue.acknowledge(start)
        await service.decide(wid, "approve-first")
        config = {"configurable": {"thread_id": wid}}
        try:
            first = await spawn_worker(tmp_path, dsn, "two_gates", block)
            children.append(first)
            await wait_for_event(tmp_path, "blocked_" + block, first)
            async with asyncio.timeout(5):
                before = await app.aget_state(config)
                while block == "between" and before.next != ("between",):
                    await asyncio.sleep(0.02)
                    before = await app.aget_state(config)
            first.kill()
            await first.communicate()
            assert first.returncode == -signal.SIGKILL
            second = await spawn_worker(tmp_path, dsn, "two_gates", "none")
            children.append(second)
            _, stderr = await asyncio.wait_for(second.communicate(), 15)
            assert second.returncode == 0, stderr.decode()
            assert second.pid != first.pid
            after = await app.aget_state(config)
            names = [item["event"] for item in events(tmp_path)]
            assert names.count("approved") == 1, names
            assert names.count("between") == 1, names
            assert names.count("approved_second") == 0, names
            assert names.count("finish") == 0, names
            assert after.interrupts[0].value == {"reason": "second_approval"}
            if block == "ack":
                assert after.interrupts[0].id == before.interrupts[0].id
            async with queue._lock:
                row = await (
                    await queue._conn.execute(
                        "SELECT status, attempts FROM workflow_jobs WHERE workflow_id=%s AND kind='resume'",
                        (wid,),
                    )
                ).fetchone()
            assert row == ("done", 2)
            await service.decide(wid, "approve-second")
            approved = await queue.dequeue("explicit-second-approval", 0.01)
            await service._invoke_job(approved)
            assert await queue.acknowledge(approved)
            assert (await app.aget_state(config)).values["phase"] == "completed"
            assert [item["event"] for item in events(tmp_path)].count(
                "approved_second"
            ) == 1
        finally:
            for child in children:
                if child.returncode is None:
                    child.kill()
                await child.communicate()
