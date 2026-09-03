"""Workflow inventory contract shared by memory and durable queue backends."""

import os
from uuid import uuid4

import pytest

from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import workflow_queue_context


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_inventory_filters_before_limit_and_deduplicates_retries(backend):
    if backend == "postgres" and os.getenv("RUN_POSTGRES_TESTS") != "1":
        pytest.skip("requires opt-in PostgreSQL test service")
    settings = Settings(
        workflow_queue_backend=backend,
        checkpointer_backend=backend,
        database_url=os.getenv(
            "TEST_DATABASE_URL", "postgresql://forge:forge@localhost:5432/forgehand"
        ),
    )
    owner = str(uuid4())
    first, second, third = (str(uuid4()) for _ in range(3))
    async with workflow_queue_context(settings) as queue:
        for wid, project, principal, kind in [
            (first, "one", owner, "start"),
            (second, "two", owner, "start"),
            (third, "one", owner, "start"),
            (first, "one", owner, "resume"),
            (str(uuid4()), "one", "other-owner", "start"),
        ]:
            await queue.enqueue(
                workflow_id=wid,
                project_id=project,
                owner_client_id=principal,
                kind=kind,
                payload={},
            )
        assert [
            w.workflow_id for w in await queue.list_workflows(owner_client_id=owner)
        ] == [third, second, first]
        assert [
            w.workflow_id
            for w in await queue.list_workflows(
                owner_client_id=owner, project_id="one", limit=1
            )
        ] == [third]
        await queue.cancel(first)
        assert [
            w.workflow_id
            for w in await queue.list_workflows(owner_client_id=owner, project_id="one")
        ] == [third, first]
        assert await queue.list_workflows(owner_client_id=owner, limit=0) == []
