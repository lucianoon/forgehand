"""Existing runtime objects recover after PostgreSQL closes their connections."""

import asyncio
import os
from uuid import uuid4

import pytest

from app.api.container import checkpointer_context
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import PostgresWorkflowQueue

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1", reason="requires disposable PostgreSQL"
)


@pytest.fixture
async def reconnection_database():
    from psycopg import AsyncConnection, sql
    from psycopg.conninfo import make_conninfo

    dsn = os.getenv("TEST_DATABASE_URL", "postgresql://forge:forge@localhost:5432/forgehand")
    schema = "reconnect_" + uuid4().hex
    async with await AsyncConnection.connect(dsn, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped = make_conninfo(dsn, options=f"-c search_path={schema}")
        try:
            yield scoped, admin
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.asyncio
async def test_queue_recovers_same_admission_after_server_disconnect(reconnection_database):
    from psycopg import OperationalError

    dsn, admin = reconnection_database
    queue = PostgresWorkflowQueue(dsn)
    await queue.setup()
    try:
        body = dict(workflow_id=str(uuid4()), project_id="p", owner_client_id="owner",
                    payload={"request": "once"}, repository="acme/repo", idempotency_key="once")
        identifier = await queue.enqueue_start(**body)
        scope = await queue.dispatch_scope()
        pid = queue._conn.info.backend_pid
        await admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        # The failed operation is returned, never hidden behind an automatic replay.
        with pytest.raises(OperationalError):
            await queue.ping()
        assert await queue.ping()
        assert queue._conn.info.backend_pid != pid
        assert await queue.dispatch_scope() == scope
        assert await queue.enqueue_start(**body) == identifier
        stats = await queue.get_stats()
        assert stats.queued == 1
        assert stats.delivered_total == 0
        job = await queue.dequeue("recovered", 0.01)
        assert job.workflow_id == identifier
        assert await queue.acknowledge(job)
        assert (await queue.get_stats()).done == 1
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_checkpointer_recovers_reads_and_writes_without_recreating_context(reconnection_database):
    from langgraph.graph import END, START, StateGraph
    from psycopg_pool import AsyncConnectionPool
    from typing import TypedDict

    dsn, admin = reconnection_database

    class State(TypedDict):
        value: int

    graph = StateGraph(State)
    graph.add_node("increment", lambda state: {"value": state["value"] + 1})
    graph.add_edge(START, "increment")
    graph.add_edge("increment", END)
    settings = Settings(_env_file=None, checkpointer_backend="postgres", database_url=dsn)
    async with checkpointer_context(settings) as saver:
        app = graph.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": str(uuid4())}}
        assert await app.ainvoke({"value": 1}, config) == {"value": 2}
        # Target the exact runtime PID, never another application's session.
        # Both the baseline dedicated saver and the repaired pool are supported.
        connection = saver.conn
        if isinstance(connection, AsyncConnectionPool):
            async with connection.connection() as borrowed:
                pid = borrowed.info.backend_pid
        else:
            pid = connection.info.backend_pid
        await admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        async with asyncio.timeout(10):
            snapshot = await app.aget_state(config)
            assert snapshot.values == {"value": 2}
            await app.aupdate_state(config, {"value": 4})
            assert (await app.aget_state(config)).values == {"value": 4}
