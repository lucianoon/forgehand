"""Concurrent PostgreSQL startup migrates once and releases its session lock."""

import asyncio
import os
from uuid import uuid4

import pytest

from app.api.container import checkpointer_context
from app.infrastructure.settings import Settings

pytestmark = pytest.mark.skipif(os.getenv("RUN_POSTGRES_TESTS") != "1", reason="requires test PostgreSQL")


@pytest.fixture
async def checkpointer_database():
    from psycopg import AsyncConnection, sql
    from psycopg.conninfo import make_conninfo

    dsn = os.getenv("TEST_DATABASE_URL", "postgresql://forge:forge@localhost:5432/forgehand")
    schema = "checkpointer_setup_" + uuid4().hex
    async with await AsyncConnection.connect(dsn, autocommit=True) as observer:
        await observer.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped = make_conninfo(dsn, options=f"-c search_path={schema}")
        settings = Settings(checkpointer_backend="postgres", database_url=scoped, _env_file=None)
        try:
            yield settings, observer, schema
        finally:
            await observer.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


async def wait_for_runtime_event(event, tasks):
    """Propagate startup failures instead of hiding them behind an event timeout."""
    waiter = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait([waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not waiter:
                task.result()
                pytest.fail("Runtime ended before its release event")
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def test_concurrent_startup_serializes_real_migrations_only(checkpointer_database, monkeypatch):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection, sql
    from psycopg.pq import TransactionStatus

    settings, observer, schema = checkpointer_database
    original_setup = AsyncPostgresSaver.setup
    original_execute = AsyncConnection.execute
    first_entered = asyncio.Event()
    all_connections_attempted_lock = asyncio.Event()
    lock_attempts = set()
    allow_migration = asyncio.Event()
    runtime_entered = asyncio.Event()
    release_runtime = asyncio.Event()
    active_setup = 0
    peak_setup = 0
    runtime_count = 0
    first_pid = None

    async def observed_execute(connection, query, *args, **kwargs):
        cursor = await original_execute(connection, query, *args, **kwargs)
        if "pg_try_advisory_lock" in str(query):
            lock_attempts.add(connection.info.backend_pid)
            if len(lock_attempts) == 3:
                all_connections_attempted_lock.set()
        return cursor

    async def observed_setup(saver):
        nonlocal active_setup, peak_setup, first_pid
        active_setup += 1
        peak_setup = max(peak_setup, active_setup)
        assert saver.conn.autocommit
        assert saver.conn.info.transaction_status == TransactionStatus.IDLE
        if first_pid is None:
            first_pid = saver.conn.info.backend_pid
            first_entered.set()
        try:
            await allow_migration.wait()
            await original_setup(saver)
        finally:
            active_setup -= 1

    async def runtime():
        nonlocal runtime_count
        async with checkpointer_context(settings):
            runtime_count += 1
            if runtime_count == 3:
                runtime_entered.set()
            await release_runtime.wait()

    monkeypatch.setattr(AsyncPostgresSaver, "setup", observed_setup)
    monkeypatch.setattr(AsyncConnection, "execute", observed_execute)
    tasks = [asyncio.create_task(runtime())]
    try:
        async with asyncio.timeout(10):
            await wait_for_runtime_event(first_entered, tasks)
            tasks.extend(asyncio.create_task(runtime()) for _ in range(2))
            await wait_for_runtime_event(all_connections_attempted_lock, tasks)
            assert active_setup == 1
            # All three connections attempted the held session lock; waiting
            # connections end their statements so concurrent indexes can finish.
            allow_migration.set()
            # No runtime may retain the setup lock: all three enter while each
            # earlier context stays open, as real API and workers do.
            await wait_for_runtime_event(runtime_entered, tasks)
            assert peak_setup == 1
            cursor = await observer.execute(
                sql.SQL("SELECT count(*) FROM {}.checkpoint_migrations").format(sql.Identifier(schema))
            )
            assert (await cursor.fetchone())[0] == len(AsyncPostgresSaver.MIGRATIONS)
            cursor = await observer.execute(
                "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND NOT i.indisvalid",
                (schema,),
            )
            assert (await cursor.fetchone())[0] == 0
            release_runtime.set()
            await asyncio.gather(*tasks)
    except TimeoutError:
        cursor = await observer.execute(
            "SELECT pid, wait_event_type, wait_event, pg_blocking_pids(pid) "
            "FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()"
        )
        pytest.fail(f"Concurrent startup timed out; backend waits: {await cursor.fetchall()}")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["failure", "cancellation"])
async def test_failed_or_cancelled_setup_releases_session_lock(checkpointer_database, monkeypatch, outcome):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    settings, _, _ = checkpointer_database
    original_setup = AsyncPostgresSaver.setup
    entered = asyncio.Event()

    async def interrupted_setup(saver):
        entered.set()
        if outcome == "failure":
            raise RuntimeError("injected migration failure")
        await asyncio.Event().wait()

    async def first_runtime():
        async with checkpointer_context(settings):
            pytest.fail("Interrupted migrations must not expose a runtime")

    monkeypatch.setattr(AsyncPostgresSaver, "setup", interrupted_setup)
    task = asyncio.create_task(first_runtime())
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            if outcome == "cancellation":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(RuntimeError, match="injected migration failure"):
                    await task
            monkeypatch.setattr(AsyncPostgresSaver, "setup", original_setup)
            async with checkpointer_context(settings):
                pass
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
