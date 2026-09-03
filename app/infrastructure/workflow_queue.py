from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Literal, cast

from app.graph.state import WorkflowPhase
from app.infrastructure.settings import Settings

JobKind = Literal["start", "resume"]
JobStatus = Literal["queued", "processing", "done", "failed"]


@dataclass(frozen=True)
class WorkflowJob:
    id: str
    workflow_id: str
    project_id: str
    owner_client_id: str
    kind: JobKind
    payload: dict[str, Any] | str
    attempt_count: int = 0
    max_attempts: int = 1
    locked_by: str | None = None


@dataclass(frozen=True)
class WorkflowJobState:
    workflow_id: str
    status: JobStatus
    error: str | None = None

    def to_pending_response(self) -> dict[str, Any]:
        phase = (
            WorkflowPhase.CANCELLED
            if self.error == "WorkflowCancelled"
            else WorkflowPhase.FAILED
            if self.status == "failed"
            else WorkflowPhase.QUEUED
        )
        return {
            "workflow_id": self.workflow_id,
            "phase": phase,
            "iteration": 0,
            "usage": {"tokens": 0, "cost_usd": 0.0},
            "tasks": [],
            "pending_decision": None,
            "final_output": (
                "Workflow cancelado pelo operador."
                if self.error == "WorkflowCancelled"
                else "Workflow falhou durante o processamento da fila."
                if self.status == "failed"
                else None
            ),
            "error": self.error,
        }


@dataclass(frozen=True)
class WorkflowAccessContext:
    workflow_id: str
    project_id: str
    owner_client_id: str


@dataclass(frozen=True)
class WorkflowQueueStats:
    backend: str
    queued: int
    processing: int
    failed: int
    done: int
    delivered_total: int
    requeued_total: int
    failed_total: int
    expired_processing: int = 0
    active_workers: int = 0

    def to_dict(self) -> dict[str, int | str]:
        return {
            "backend": self.backend,
            "queued": self.queued,
            "processing": self.processing,
            "failed": self.failed,
            "done": self.done,
            "delivered_total": self.delivered_total,
            "requeued_total": self.requeued_total,
            "failed_total": self.failed_total,
            "expired_processing": self.expired_processing,
            "active_workers": self.active_workers,
        }


def _lease_expired_error() -> str:
    return "LeaseExpired"


def _serialize_payload(payload: dict[str, Any] | str) -> str:
    def encode(value: Any) -> Any:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return model_dump(mode="json")
        raise TypeError(f"{type(value).__name__} não é serializável como JSON")

    return json.dumps(payload, default=encode)


class InMemoryWorkflowQueue:
    def __init__(
        self, lease_seconds: float = 30.0, max_delivery_attempts: int = 3
    ) -> None:
        self._lease_seconds = lease_seconds
        self._max_delivery_attempts = max_delivery_attempts
        self._queued: list[WorkflowJob] = []
        self._processing: dict[str, tuple[WorkflowJob, float]] = {}
        self._states: dict[str, WorkflowJobState] = {}
        self._access: dict[str, WorkflowAccessContext] = {}
        self._condition = asyncio.Condition()
        self._delivered_total = 0
        self._requeued_total = 0
        self._failed_total = 0
        self._done_total = 0
        self._sequence = 0
        self._worker_heartbeats: dict[str, float] = {}
        self._idempotency: dict[tuple[str, str, str], str] = {}
        self._work_orders: dict[str, dict[str, Any]] = {}

    async def claim_idempotency(
        self,
        *,
        owner_client_id: str,
        repository: str,
        idempotency_key: str,
        workflow_id: str,
    ) -> tuple[str, bool]:
        scope = (owner_client_id, repository.lower(), idempotency_key)
        async with self._condition:
            existing = self._idempotency.get(scope)
            if existing is not None:
                return existing, False
            self._idempotency[scope] = workflow_id
            return workflow_id, True

    async def touch_worker(self, worker_id: str) -> None:
        async with self._condition:
            self._worker_heartbeats[worker_id] = time.monotonic()

    async def enqueue(
        self,
        workflow_id: str,
        project_id: str,
        owner_client_id: str,
        kind: JobKind,
        payload: dict[str, Any] | str,
    ) -> WorkflowJob:
        self._sequence += 1
        job = WorkflowJob(
            id=str(self._sequence),
            workflow_id=workflow_id,
            project_id=project_id,
            owner_client_id=owner_client_id,
            kind=kind,
            payload=payload,
            attempt_count=0,
            max_attempts=self._max_delivery_attempts,
        )
        self._access[workflow_id] = WorkflowAccessContext(
            workflow_id=workflow_id,
            project_id=project_id,
            owner_client_id=owner_client_id,
        )
        async with self._condition:
            self._states[workflow_id] = WorkflowJobState(
                workflow_id=workflow_id, status="queued"
            )
            if kind == "start" and isinstance(payload, dict):
                work_order = payload.get("work_order")
                model_dump = getattr(work_order, "model_dump", None)
                if callable(model_dump):
                    self._work_orders[workflow_id] = model_dump(mode="json")
                elif isinstance(work_order, dict):
                    self._work_orders[workflow_id] = work_order
            self._queued.append(job)
            self._condition.notify_all()
        return job

    def _requeue_expired_locked(self, now: float) -> None:
        expired: list[str] = []
        for workflow_id, (job, locked_at) in self._processing.items():
            if now - locked_at < self._lease_seconds:
                continue
            expired.append(workflow_id)
            if job.attempt_count >= job.max_attempts:
                self._states[workflow_id] = WorkflowJobState(
                    workflow_id=workflow_id,
                    status="failed",
                    error=_lease_expired_error(),
                )
                self._failed_total += 1
                continue
            self._states[workflow_id] = WorkflowJobState(
                workflow_id=workflow_id,
                status="queued",
                error=_lease_expired_error(),
            )
            self._queued.append(job)
            self._requeued_total += 1
        for workflow_id in expired:
            self._processing.pop(workflow_id, None)
        if expired:
            self._condition.notify_all()

    async def dequeue(
        self, worker_id: str, poll_interval_seconds: float
    ) -> WorkflowJob | None:
        deadline = time.monotonic() + poll_interval_seconds
        async with self._condition:
            while True:
                self._requeue_expired_locked(time.monotonic())
                if self._queued:
                    job = self._queued.pop(0)
                    leased_job = WorkflowJob(
                        id=job.id,
                        workflow_id=job.workflow_id,
                        project_id=job.project_id,
                        owner_client_id=job.owner_client_id,
                        kind=job.kind,
                        payload=job.payload,
                        attempt_count=job.attempt_count + 1,
                        max_attempts=job.max_attempts,
                        locked_by=worker_id,
                    )
                    self._processing[leased_job.workflow_id] = (
                        leased_job,
                        time.monotonic(),
                    )
                    self._states[leased_job.workflow_id] = WorkflowJobState(
                        workflow_id=leased_job.workflow_id,
                        status="processing",
                    )
                    self._delivered_total += 1
                    return leased_job
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return None

    async def heartbeat(self, job: WorkflowJob) -> bool:
        async with self._condition:
            current = self._processing.get(job.workflow_id)
            if current is None or current[0].locked_by != job.locked_by:
                return False
            self._processing[job.workflow_id] = (current[0], time.monotonic())
            return True

    async def acknowledge(self, job: WorkflowJob) -> bool:
        async with self._condition:
            current = self._processing.get(job.workflow_id)
            if current is None or current[0].locked_by != job.locked_by:
                return False
            self._processing.pop(job.workflow_id)
            self._states.pop(job.workflow_id, None)
            self._done_total += 1
            self._condition.notify_all()
            return True

    async def fail(self, job: WorkflowJob, error: str) -> bool:
        async with self._condition:
            current = self._processing.get(job.workflow_id)
            if current is None or current[0].locked_by != job.locked_by:
                return False
            self._processing.pop(job.workflow_id)
            self._states[job.workflow_id] = WorkflowJobState(
                workflow_id=job.workflow_id,
                status="failed",
                error=error,
            )
            self._failed_total += 1
            self._condition.notify_all()
            return True

    async def cancel(self, workflow_id: str) -> bool:
        async with self._condition:
            queued = any(job.workflow_id == workflow_id for job in self._queued)
            processing = self._processing.pop(workflow_id, None) is not None
            if not queued and not processing:
                return False
            self._queued = [
                job for job in self._queued if job.workflow_id != workflow_id
            ]
            self._states[workflow_id] = WorkflowJobState(
                workflow_id=workflow_id,
                status="failed",
                error="WorkflowCancelled",
            )
            self._failed_total += 1
            self._condition.notify_all()
            return True

    async def get_state(self, workflow_id: str) -> WorkflowJobState | None:
        return self._states.get(workflow_id)

    async def get_access(self, workflow_id: str) -> WorkflowAccessContext | None:
        return self._access.get(workflow_id)

    async def get_work_order(self, workflow_id: str) -> dict[str, Any] | None:
        return self._work_orders.get(workflow_id)

    async def ping(self) -> bool:
        return True

    async def get_stats(self) -> WorkflowQueueStats:
        async with self._condition:
            worker_cutoff = time.monotonic() - max(self._lease_seconds * 2, 1.0)
            return WorkflowQueueStats(
                backend="memory",
                queued=len(self._queued),
                processing=len(self._processing),
                failed=sum(
                    1 for state in self._states.values() if state.status == "failed"
                ),
                done=self._done_total,
                delivered_total=self._delivered_total,
                requeued_total=self._requeued_total,
                failed_total=self._failed_total,
                expired_processing=sum(
                    1
                    for _, locked_at in self._processing.values()
                    if time.monotonic() - locked_at >= self._lease_seconds
                ),
                active_workers=sum(
                    heartbeat >= worker_cutoff
                    for heartbeat in self._worker_heartbeats.values()
                ),
            )

    async def close(self) -> None:
        return None


class PostgresWorkflowQueue:
    def __init__(
        self, dsn: str, lease_seconds: float = 30.0, max_delivery_attempts: int = 3
    ) -> None:
        self._dsn = dsn
        self._lease_seconds = lease_seconds
        self._max_delivery_attempts = max_delivery_attempts
        self._conn: Any | None = None
        self._lock = asyncio.Lock()

    async def setup(self) -> None:
        from psycopg import AsyncConnection

        self._conn = await AsyncConnection.connect(self._dsn)
        async with self._lock:
            await self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_jobs (
                    id BIGSERIAL PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    owner_client_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('start', 'resume')),
                    payload JSONB NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('queued', 'processing', 'done', 'failed')),
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    locked_by TEXT,
                    locked_at TIMESTAMPTZ,
                    retry_after TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_workflow_jobs_ready
                    ON workflow_jobs (status, id)
                """
            )
            await self._conn.execute(
                "ALTER TABLE workflow_jobs ADD COLUMN IF NOT EXISTS project_id TEXT"
            )
            await self._conn.execute(
                "ALTER TABLE workflow_jobs ADD COLUMN IF NOT EXISTS owner_client_id TEXT"
            )
            await self._conn.execute(
                """
                ALTER TABLE workflow_jobs
                ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3
                """
            )
            await self._conn.execute(
                """
                ALTER TABLE workflow_jobs
                ADD COLUMN IF NOT EXISTS retry_after TIMESTAMPTZ NOT NULL DEFAULT NOW()
                """
            )
            await self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_workers (
                    worker_id TEXT PRIMARY KEY,
                    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_idempotency (
                    owner_client_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (owner_client_id, repository, idempotency_key)
                )
                """
            )
            await self._conn.commit()

    async def claim_idempotency(
        self,
        *,
        owner_client_id: str,
        repository: str,
        idempotency_key: str,
        workflow_id: str,
    ) -> tuple[str, bool]:
        assert self._conn is not None
        repository = repository.lower()
        async with self._lock:
            cur = await self._conn.execute(
                """
                INSERT INTO workflow_idempotency (
                    owner_client_id, repository, idempotency_key, workflow_id
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (owner_client_id, repository, idempotency_key)
                DO NOTHING
                RETURNING workflow_id
                """,
                (owner_client_id, repository, idempotency_key, workflow_id),
            )
            inserted = await cur.fetchone()
            if inserted is not None:
                await self._conn.commit()
                return str(inserted[0]), True
            cur = await self._conn.execute(
                """
                SELECT workflow_id
                FROM workflow_idempotency
                WHERE owner_client_id = %s
                  AND repository = %s
                  AND idempotency_key = %s
                """,
                (owner_client_id, repository, idempotency_key),
            )
            existing = await cur.fetchone()
            await self._conn.commit()
        if existing is None:
            raise RuntimeError("Falha ao recuperar chave de idempotência.")
        return str(existing[0]), False

    async def touch_worker(self, worker_id: str) -> None:
        assert self._conn is not None
        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO workflow_workers (worker_id, heartbeat_at)
                VALUES (%s, NOW())
                ON CONFLICT (worker_id)
                DO UPDATE SET heartbeat_at = EXCLUDED.heartbeat_at
                """,
                (worker_id,),
            )
            await self._conn.commit()

    async def enqueue(
        self,
        workflow_id: str,
        project_id: str,
        owner_client_id: str,
        kind: JobKind,
        payload: dict[str, Any] | str,
    ) -> WorkflowJob:
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute(
                """
                INSERT INTO workflow_jobs (
                    workflow_id,
                    project_id,
                    owner_client_id,
                    kind,
                    payload,
                    status,
                    max_attempts,
                    retry_after
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, 'queued', %s, NOW())
                RETURNING id
                """,
                (
                    workflow_id,
                    project_id,
                    owner_client_id,
                    kind,
                    _serialize_payload(payload),
                    self._max_delivery_attempts,
                ),
            )
            row = await cur.fetchone()
            await self._conn.commit()
        return WorkflowJob(
            id=str(row[0]),
            workflow_id=workflow_id,
            project_id=project_id,
            owner_client_id=owner_client_id,
            kind=kind,
            payload=payload,
            attempt_count=0,
            max_attempts=self._max_delivery_attempts,
        )

    async def _requeue_expired_locked(self) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """
            UPDATE workflow_jobs
            SET status = CASE
                    WHEN attempts >= COALESCE(max_attempts, %s) THEN 'failed'
                    ELSE 'queued'
                END,
                error = CASE
                    WHEN attempts >= COALESCE(max_attempts, %s)
                        THEN COALESCE(error, %s)
                    ELSE %s
                END,
                locked_by = NULL,
                locked_at = NULL,
                retry_after = NOW(),
                updated_at = NOW()
            WHERE status = 'processing'
              AND locked_at IS NOT NULL
              AND locked_at <= NOW() - (%s * INTERVAL '1 second')
            """,
            (
                self._max_delivery_attempts,
                self._max_delivery_attempts,
                _lease_expired_error(),
                _lease_expired_error(),
                self._lease_seconds,
            ),
        )

    async def dequeue(
        self, worker_id: str, poll_interval_seconds: float
    ) -> WorkflowJob | None:
        assert self._conn is not None
        async with self._lock:
            async with self._conn.transaction():
                await self._requeue_expired_locked()
                cur = await self._conn.execute(
                    """
                    WITH next_job AS (
                        SELECT id
                        FROM workflow_jobs
                        WHERE status = 'queued'
                          AND retry_after <= NOW()
                        ORDER BY id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE workflow_jobs AS jobs
                    SET status = 'processing',
                        attempts = attempts + 1,
                        locked_by = %s,
                        locked_at = NOW(),
                        updated_at = NOW()
                    FROM next_job
                    WHERE jobs.id = next_job.id
                    RETURNING
                        jobs.id,
                        jobs.workflow_id,
                        jobs.project_id,
                        jobs.owner_client_id,
                        jobs.kind,
                        jobs.payload,
                        jobs.attempts,
                        jobs.max_attempts,
                        jobs.locked_by
                    """,
                    (worker_id,),
                )
                row = await cur.fetchone()
            await self._conn.commit()
        if row is None:
            await asyncio.sleep(poll_interval_seconds)
            return None
        payload = row[5]
        return WorkflowJob(
            id=str(row[0]),
            workflow_id=row[1],
            project_id=row[2],
            owner_client_id=row[3],
            kind=row[4],
            payload=payload,
            attempt_count=row[6],
            max_attempts=row[7],
            locked_by=row[8],
        )

    async def heartbeat(self, job: WorkflowJob) -> bool:
        assert self._conn is not None
        if job.locked_by is None:
            return False
        async with self._lock:
            cur = await self._conn.execute(
                """
                UPDATE workflow_jobs
                SET locked_at = NOW(), updated_at = NOW()
                WHERE id = %s
                  AND status = 'processing'
                  AND locked_by = %s
                RETURNING id
                """,
                (int(job.id), job.locked_by),
            )
            row = await cur.fetchone()
            await self._conn.commit()
        return row is not None

    async def acknowledge(self, job: WorkflowJob) -> bool:
        assert self._conn is not None
        if job.locked_by is None:
            return False
        async with self._lock:
            cur = await self._conn.execute(
                """
                UPDATE workflow_jobs
                SET status = 'done',
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = NOW()
                WHERE id = %s
                  AND status = 'processing'
                  AND locked_by = %s
                RETURNING id
                """,
                (int(job.id), job.locked_by),
            )
            row = await cur.fetchone()
            await self._conn.commit()
        return row is not None

    async def fail(self, job: WorkflowJob, error: str) -> bool:
        assert self._conn is not None
        if job.locked_by is None:
            return False
        async with self._lock:
            cur = await self._conn.execute(
                """
                UPDATE workflow_jobs
                SET status = 'failed',
                    error = %s,
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = NOW()
                WHERE id = %s
                  AND status = 'processing'
                  AND locked_by = %s
                RETURNING id
                """,
                (error, int(job.id), job.locked_by),
            )
            row = await cur.fetchone()
            await self._conn.commit()
        return row is not None

    async def cancel(self, workflow_id: str) -> bool:
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute(
                """
                UPDATE workflow_jobs
                SET status = 'failed',
                    error = 'WorkflowCancelled',
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = NOW()
                WHERE workflow_id = %s AND status IN ('queued', 'processing')
                RETURNING id
                """,
                (workflow_id,),
            )
            row = await cur.fetchone()
            await self._conn.commit()
        return row is not None

    async def get_state(self, workflow_id: str) -> WorkflowJobState | None:
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute(
                """
                SELECT status, error
                FROM workflow_jobs
                WHERE workflow_id = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (workflow_id,),
            )
            row = await cur.fetchone()
            await self._conn.commit()
        if row is None:
            return None
        return WorkflowJobState(
            workflow_id=workflow_id,
            status=row[0],
            error=row[1],
        )

    async def get_access(self, workflow_id: str) -> WorkflowAccessContext | None:
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute(
                """
                SELECT project_id, owner_client_id
                FROM workflow_jobs
                WHERE workflow_id = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (workflow_id,),
            )
            row = await cur.fetchone()
            await self._conn.commit()
        if row is None:
            return None
        return WorkflowAccessContext(
            workflow_id=workflow_id,
            project_id=row[0],
            owner_client_id=row[1],
        )

    async def get_work_order(self, workflow_id: str) -> dict[str, Any] | None:
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute(
                """
                SELECT payload -> 'work_order'
                FROM workflow_jobs
                WHERE workflow_id = %s AND kind = 'start'
                ORDER BY id ASC
                LIMIT 1
                """,
                (workflow_id,),
            )
            row = await cur.fetchone()
            await self._conn.commit()
        if row is None or not isinstance(row[0], dict):
            return None
        return cast(dict[str, Any], row[0])

    async def ping(self) -> bool:
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute("SELECT 1")
            row = await cur.fetchone()
            await self._conn.commit()
        return row is not None and row[0] == 1

    async def get_stats(self) -> WorkflowQueueStats:
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'queued') AS queued,
                    COUNT(*) FILTER (WHERE status = 'processing') AS processing,
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                    COUNT(*) FILTER (WHERE status = 'done') AS done,
                    COALESCE(SUM(attempts), 0) AS delivered_total,
                    COALESCE(SUM(GREATEST(attempts - 1, 0)), 0) AS requeued_total,
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed_total,
                    COUNT(*) FILTER (
                        WHERE status = 'processing'
                          AND locked_at IS NOT NULL
                          AND locked_at <= NOW() - (%s * INTERVAL '1 second')
                    ) AS expired_processing
                FROM workflow_jobs
                """,
                (self._lease_seconds,),
            )
            row = await cur.fetchone()
            workers_cur = await self._conn.execute(
                """
                SELECT COUNT(*)
                FROM workflow_workers
                WHERE heartbeat_at >= NOW() - (%s * INTERVAL '1 second')
                """,
                (max(self._lease_seconds * 2, 1.0),),
            )
            workers_row = await workers_cur.fetchone()
            await self._conn.commit()
        return WorkflowQueueStats(
            backend="postgres",
            queued=int(row[0]),
            processing=int(row[1]),
            failed=int(row[2]),
            done=int(row[3]),
            delivered_total=int(row[4]),
            requeued_total=int(row[5]),
            failed_total=int(row[6]),
            expired_processing=int(row[7]),
            active_workers=int(workers_row[0]),
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


@asynccontextmanager
async def workflow_queue_context(settings: Settings) -> AsyncGenerator[Any, None]:
    if settings.workflow_queue_backend == "postgres":
        queue = PostgresWorkflowQueue(
            settings.database_url,
            lease_seconds=settings.workflow_queue_lease_seconds,
            max_delivery_attempts=settings.workflow_queue_max_delivery_attempts,
        )
        await queue.setup()
        try:
            yield queue
        finally:
            await queue.close()
    else:
        yield InMemoryWorkflowQueue(
            lease_seconds=settings.workflow_queue_lease_seconds,
            max_delivery_attempts=settings.workflow_queue_max_delivery_attempts,
        )
