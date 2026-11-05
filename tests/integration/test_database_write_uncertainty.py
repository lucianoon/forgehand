"""Connection recovery cannot turn uncertain writes into automatic duplicates."""

import os
from uuid import uuid4

import pytest

from app.infrastructure.workflow_queue import PostgresWorkflowQueue
from tests.integration.test_database_reconnection import reconnection_database as reconnection_database

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1", reason="requires disposable PostgreSQL"
)


class InterruptedCommit:
    def __init__(self, connection, committed):
        self.connection = connection
        self.committed = committed
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self.connection, name)

    async def commit(self):
        from psycopg import OperationalError

        self.calls += 1
        if self.committed:
            await self.connection.commit()
        raise OperationalError("connection lost before commit result reached caller")


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_queue_does_not_replay_uncertain_commit(reconnection_database, committed):
    from psycopg import OperationalError

    dsn, _ = reconnection_database
    queue = PostgresWorkflowQueue(dsn)
    await queue.setup()
    interrupted = InterruptedCommit(queue._conn, committed)
    queue._conn = interrupted
    try:
        with pytest.raises(OperationalError):
            await queue.enqueue(str(uuid4()), "p", "owner", "start", {"request": "once"})
        assert interrupted.calls == 1
        # The next operation reconnects, exposing the durable result. It cannot
        # infer whether to enqueue again; admission with a stable identity owns
        # that decision, never this low-level connection recovery mechanism.
        assert (await queue.get_stats()).queued == int(committed)
        assert interrupted.calls == 1
    finally:
        await queue.close()
