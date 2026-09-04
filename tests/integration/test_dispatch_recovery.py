"""Atomic admission contract, also exercised against an isolated PostgreSQL schema.

Set RUN_POSTGRES_TESTS=1 and TEST_DATABASE_URL to include the durable backend.
No model calls, repository writes or worker execution are used here.
"""

import asyncio
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.agents.product import ProductStudio
from app.api.service import WorkflowService
from app.factory.product_delivery import IncrementalDelivery
from app.infrastructure.audit import InMemoryAuditLog
from app.infrastructure.product_delivery_store import ProductDeliveryStore
from app.infrastructure.product_store import ProductConflict, ProductStore
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import (
    InMemoryWorkflowQueue,
    PostgresWorkflowQueue,
    WorkflowDispatchConflict,
)
from app.main import create_app
from app.models.product_delivery import RecoverDelivery
from tests.unit.test_product_delivery import SCM, approved, build_profiles, setup


@pytest.fixture(params=["memory", "postgres"])
async def queues(request):
    if request.param == "memory":
        queue = InMemoryWorkflowQueue()
        yield queue, queue, None
        return
    if os.getenv("RUN_POSTGRES_TESTS") != "1":
        pytest.skip("RUN_POSTGRES_TESTS=1 and TEST_DATABASE_URL required")
    from psycopg import AsyncConnection, sql
    from psycopg.conninfo import make_conninfo

    dsn = os.getenv(
        "TEST_DATABASE_URL", "postgresql://forge:forge@localhost:5432/forgehand"
    )
    schema = "dispatch_test_" + uuid4().hex
    async with await AsyncConnection.connect(dsn, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped = make_conninfo(dsn, options=f"-c search_path={schema}")
        first, second = PostgresWorkflowQueue(scoped), PostgresWorkflowQueue(scoped)
        try:
            await first.setup()
            await second.setup()
            yield first, second, scoped
        finally:
            await first.close()
            await second.close()
            # Only this test-created, unique schema; never the application's tables.
            await admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def submission(**updates):
    return {
        "workflow_id": str(uuid4()),
        "project_id": "p",
        "owner_client_id": "owner",
        "payload": {
            "request": "Implement the approved requirement",
            "budget": {"max_cost_usd": 0.2},
        },
        "repository": "acme/repo",
        "idempotency_key": "approved-key",
        **updates,
    }


async def count_jobs(queue):
    if isinstance(queue, InMemoryWorkflowQueue):
        return len(queue._queued)
    async with queue._lock:
        async with queue._conn.transaction():
            cursor = await queue._conn.execute("SELECT count(*) FROM workflow_jobs")
            return (await cursor.fetchone())[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["done", "failed", "cancelled"])
async def test_concurrent_atomic_admission_and_terminal_replay(queues, terminal):
    first, second, _ = queues
    body = submission()
    results = await asyncio.gather(
        *(
            queue.enqueue_start(**{**body, "workflow_id": str(uuid4())})
            for queue in [first, second] * 8
        )
    )
    assert len(set(results)) == 1
    assert await count_jobs(first) == 1
    job = await first.dequeue("worker", 0.01)
    assert job.workflow_id == results[0]
    assert job.payload["workflow_id"] == job.workflow_id
    if terminal == "done":
        assert await first.acknowledge(job)
    elif terminal == "failed":
        assert await first.fail(job, "test-failure")
    else:
        assert await first.cancel(job.workflow_id)
    assert await second.enqueue_start(**body) == results[0]
    assert await second.dequeue("another-worker", 0.01) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"project_id": "another-project"},
        {"payload": {"request": "secret-change", "budget": {"max_cost_usd": 5}}},
    ],
)
async def test_conflicting_key_fails_closed(queues, change):
    first, second, _ = queues
    body = submission()
    await first.enqueue_start(**body)
    with pytest.raises(WorkflowDispatchConflict) as exc:
        await second.enqueue_start(**{**body, **change})
    assert "secret-change" not in str(exc.value)
    assert await count_jobs(first) == 1


@pytest.mark.asyncio
async def test_workflow_identity_deduplicates_without_key_and_checks_owner(queues):
    first, second, _ = queues
    body = submission(idempotency_key=None)
    await first.enqueue_start(**body)
    assert await second.enqueue_start(**body) == body["workflow_id"]
    with pytest.raises(WorkflowDispatchConflict):
        await second.enqueue_start(**{**body, "owner_client_id": "other"})
    assert await count_jobs(first) == 1


@pytest.mark.asyncio
async def test_legacy_claim_and_serialization_failure_never_fake_success(queues):
    first, second, _ = queues
    body = submission()
    with pytest.raises(TypeError):
        await first.enqueue_start(**{**body, "payload": {"unserializable": object()}})
    await first.claim_idempotency(
        **{
            k: body[k]
            for k in ("owner_client_id", "repository", "idempotency_key", "workflow_id")
        }
    )
    with pytest.raises(WorkflowDispatchConflict):
        await second.enqueue_start(**body)
    assert await count_jobs(first) == 0


@pytest.mark.asyncio
async def test_namespace_guard_precedes_admission(queues):
    first, second, _ = queues
    assert await first.dispatch_scope() == await second.dispatch_scope()
    body = submission(expected_dispatch_scope=str(uuid4()))
    with pytest.raises(WorkflowDispatchConflict):
        await first.enqueue_start(**body)
    assert await count_jobs(first) == 0
    body["expected_dispatch_scope"] = await second.dispatch_scope()
    assert await second.enqueue_start(**body) == body["workflow_id"]


@pytest.mark.asyncio
async def test_rotated_namespace_blocks_even_an_existing_receipt(queues):
    first, second, dsn = queues
    body = submission(expected_dispatch_scope=await first.dispatch_scope())
    await first.enqueue_start(**body)
    replacement = str(uuid4())
    if dsn:
        async with second._conn.transaction():
            await second._conn.execute(
                "UPDATE workflow_dispatch_identity SET namespace=%s WHERE singleton",
                (replacement,),
            )
    else:
        first._dispatch_scope = replacement
    with pytest.raises(WorkflowDispatchConflict):
        await first.enqueue_start(**body)
    assert await count_jobs(first) == 1


@pytest.mark.asyncio
async def test_admitted_payload_is_detached_from_caller(queues):
    first, _, _ = queues
    body = submission()
    await first.enqueue_start(**body)
    body["payload"]["budget"]["max_cost_usd"] = 999
    job = await first.dequeue("worker", 0.01)
    assert job.payload["budget"]["max_cost_usd"] == 0.2


@pytest.mark.asyncio
async def test_reservation_and_dispatch_metadata_roll_back_together(tmp_path):
    product, store, _ = setup(tmp_path)
    queue = InMemoryWorkflowQueue()
    with store.products.connection() as db:
        db.execute("""CREATE TRIGGER reject_dispatch_intent
            BEFORE INSERT ON product_delivery_dispatch_intents
            BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await IncrementalDelivery(store, workflow_service(queue)).start(
            product["id"], "owner", approved(1), SCM()
        )
    unchanged = store.get(product["id"], "owner")
    assert unchanged["revision"] == 1
    assert unchanged["features"][0]["attempts"] == []
    with store.products.connection() as db:
        assert (
            db.execute("SELECT count(*) FROM product_delivery_attempts").fetchone()[0]
            == 0
        )
    assert await count_jobs(queue) == 0


class FailInsert:
    def __init__(self, connection, mode):
        self.connection = connection
        self.mode = mode

    def __getattr__(self, name):
        return getattr(self.connection, name)

    async def execute(self, query, *args, **kwargs):
        if "INSERT INTO workflow_jobs" in str(query):
            if self.mode == "cancelled":
                raise asyncio.CancelledError()
            if self.mode == "database":
                return await self.connection.execute("SELECT 1/0")
            raise RuntimeError("injected-before-job-insert")
        return await self.connection.execute(query, *args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exception", "database", "cancelled"])
async def test_postgres_rolls_back_key_and_receipt_on_insertion_failure(queues, mode):
    first, second, dsn = queues
    if dsn is None:
        pytest.skip("PostgreSQL transaction fault injection")
    body = submission()
    original = first._conn
    first._conn = FailInsert(original, mode)
    try:
        error = asyncio.CancelledError if mode == "cancelled" else Exception
        with pytest.raises(error):
            await first.enqueue_start(**body)
    finally:
        first._conn = original
    for table in ("workflow_idempotency", "workflow_start_receipts", "workflow_jobs"):
        async with original.transaction():
            cursor = await original.execute(f"SELECT count(*) FROM {table}")
            assert (await cursor.fetchone())[0] == 0
    # The same connection must have rolled back and remain usable.
    assert await first.enqueue_start(**body) == body["workflow_id"]
    assert await second.enqueue_start(**body) == body["workflow_id"]


def workflow_service(queue):
    class EmptyGraph:
        async def aget_state(self, config):
            return SimpleNamespace(values={})

    return WorkflowService(EmptyGraph(), Settings(_env_file=None), queue, False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["before_admission", "after_admission", "cancelled"]
)
async def test_saved_intent_recovers_same_attempt_after_service_restart(
    queues, tmp_path, failure
):
    first, second, dsn = queues
    product, store, _ = setup(tmp_path)
    workflows = workflow_service(first)
    original_start = workflows.start

    async def interrupted(**kwargs):
        if failure == "after_admission":
            await original_start(**kwargs)
        if failure == "cancelled":
            raise asyncio.CancelledError()
        raise RuntimeError("secret-provider-body")

    workflows.start = interrupted
    service = IncrementalDelivery(store, workflows)
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await service.start(product["id"], "owner", approved(1), SCM())
        uncertain = store.get(product["id"], "owner")
    else:
        uncertain = await service.start(product["id"], "owner", approved(1), SCM())
    assert "secret-provider-body" not in json.dumps(uncertain)
    attempt = uncertain["features"][0]["attempts"][0]
    wid = attempt["workflow_id"]
    context = store.context(product["id"], "owner", wid)
    order, namespace = store.dispatch_intent(product["id"], "owner", wid)
    if dsn:
        # New queue object/connection, not just a replacement service instance.
        await second.close()
        await second.setup()
    restored = IncrementalDelivery(
        ProductDeliveryStore(ProductStore(store.products.path)),
        workflow_service(second),
    )
    recovery = RecoverDelivery(
        revision=uncertain["revision"], approved=True, workflow_id=wid
    )
    results = await asyncio.gather(
        *(restored.recover(product["id"], "owner", recovery) for _ in range(5)),
        return_exceptions=True,
    )
    assert any(isinstance(result, dict) for result in results)
    assert all(isinstance(result, (dict, ProductConflict)) for result in results)
    recovered = store.get(product["id"], "owner")
    assert len(recovered["features"][0]["attempts"]) == 1
    assert recovered["features"][0]["status"] == "running"
    assert await count_jobs(second) == 1
    job = await second.dequeue("worker", 0.01)
    assert job.workflow_id == wid
    queued_order = job.payload["work_order"]
    queued_budget = job.payload["budget"]
    if hasattr(queued_order, "model_dump"):
        queued_order = queued_order.model_dump(mode="json")
        queued_budget = queued_budget.model_dump(mode="json")
    assert queued_order == order
    assert queued_budget["max_cost_usd"] == 0.2
    assert await second.dispatch_scope() == namespace
    assert store.context(product["id"], "owner", wid) == context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocker",
    ["replacement", "legacy", "corrupt", "stale", "wrong_workflow", "terminal"],
)
async def test_recovery_refuses_unsafe_intents(tmp_path, blocker):
    product, store, _ = setup(tmp_path)
    queue = InMemoryWorkflowQueue()
    workflows = workflow_service(queue)

    async def fail(**kwargs):
        raise RuntimeError("unavailable")

    workflows.start = fail
    unknown = await IncrementalDelivery(store, workflows).start(
        product["id"], "owner", approved(1), SCM()
    )
    wid = unknown["features"][0]["attempts"][0]["workflow_id"]
    revision = unknown["revision"]
    if blocker in {"legacy", "corrupt"}:
        with store.products.connection() as db:
            if blocker == "legacy":
                db.execute(
                    "DELETE FROM product_delivery_dispatch_intents WHERE workflow_id=?",
                    (wid,),
                )
            else:
                db.execute(
                    "UPDATE product_delivery_attempts SET work_order='{}' WHERE workflow_id=?",
                    (wid,),
                )
    if blocker == "terminal":
        unknown = store.update_attempt(product["id"], "owner", revision, "failed", {})
        revision = unknown["revision"]
    if blocker == "replacement":
        queue = InMemoryWorkflowQueue()
    service = IncrementalDelivery(store, workflow_service(queue))
    body = RecoverDelivery(
        revision=1 if blocker == "stale" else revision,
        approved=True,
        workflow_id=str(uuid4()) if blocker == "wrong_workflow" else wid,
    )
    with pytest.raises(ProductConflict):
        await service.recover(product["id"], "owner", body)
    assert await count_jobs(queue) == 0


@pytest.mark.asyncio
async def test_recovery_api_authorization_preflight_and_audit(tmp_path, monkeypatch):
    product, store, _ = setup(tmp_path)
    queue = InMemoryWorkflowQueue()
    workflows = workflow_service(queue)
    original = workflows.start

    async def fail(**kwargs):
        raise RuntimeError("secret-body")

    workflows.start = fail
    uncertain = await IncrementalDelivery(store, workflows).start(
        product["id"], "owner", approved(1), SCM()
    )
    workflows.start = original

    async def ready():
        return {"ready": True, "queue_ready": True, "embedded_workers_enabled": True}

    workflows.readiness = ready
    app = create_app()
    app.state.settings = Settings(
        _env_file=None,
        factory_build_profiles_json=build_profiles(),
        api_keys_json=json.dumps(
            {
                "owner": {"client_id": "owner", "role": "approver", "projects": ["p"]},
                "viewer": {"client_id": "owner", "role": "viewer", "projects": ["p"]},
                "other": {"client_id": "other", "role": "approver", "projects": ["p"]},
                "wrong-project": {
                    "client_id": "owner",
                    "role": "approver",
                    "projects": ["q"],
                },
            }
        ),
    )
    audit = InMemoryAuditLog()
    app.state.container = SimpleNamespace(
        product_studio=ProductStudio(None, store.products),
        workflow_service=workflows,
        audit_log=audit,
    )
    monkeypatch.setattr(
        "app.factory.preflight.github_credential_configured", lambda: True
    )
    path = f"/products/{product['id']}/delivery"
    wid = uncertain["features"][0]["attempts"][0]["workflow_id"]
    body = {"revision": uncertain["revision"], "workflow_id": wid, "approved": True}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for key, status in [
            (None, 401),
            ("viewer", 403),
            ("other", 404),
            ("wrong-project", 403),
        ]:
            response = await client.post(
                path + "/recover", json=body, headers={"X-API-Key": key} if key else {}
            )
            assert response.status_code == status
        headers = {"X-API-Key": "owner"}
        assert (
            await client.post(path + "/recover", json=body, headers=headers)
        ).status_code == 409
        app.state.settings = app.state.settings.model_copy(
            update={"factory_mode_enabled": True}
        )
        for invalid in [
            {**body, "approved": False},
            {**body, "max_cost_usd": 5},
            {k: v for k, v in body.items() if k != "approved"},
        ]:
            assert (
                await client.post(path + "/recover", json=invalid, headers=headers)
            ).status_code == 422
        assert (await client.get(path, headers=headers)).status_code == 200
        assert await count_jobs(queue) == 0
        workflows.start = fail
        failure = await client.post(path + "/recover", json=body, headers=headers)
        assert failure.status_code == 202
        assert failure.json()["plan"]["features"][0]["status"] == "dispatch_unknown"
        assert "secret-body" not in failure.text
        assert await count_jobs(queue) == 0
        workflows.start = original
        response = await client.post(path + "/recover", json=body, headers=headers)
        assert response.status_code == 202, response.text
        assert response.json()["plan"]["features"][0]["status"] == "running"
        assert await count_jobs(queue) == 1
        assert (
            await client.post(path + "/recover", json=body, headers=headers)
        ).status_code == 409
        events = await audit.list_recent(limit=100)
        assert any(
            e.action == "product_delivery_recover" and e.outcome == "requested"
            for e in events
        )
        assert any(
            e.action == "product_delivery_recover" and e.outcome == "processed"
            for e in events
        )
