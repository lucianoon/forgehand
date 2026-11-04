"""Persistência unificada da camada Super-Agent (SQLite / In-Memory).

Armazena:
- RouterTickets (decisões do Router, modo observe e vínculo com workflow_id)
- ThreadSubscriptions (Subscription Direct: bypass do Router para Threads ativas)
- OutboxMessages (fila persistente idempotente com retry e backoff exponencial)
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from app.models.superagent import (
    OutboxDestinationKind,
    OutboxMessage,
    OutboxStatus,
    RouterDecisionStatus,
    RouterTicket,
    ThreadSubscription,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SuperAgentStore:
    """Store transacional em SQLite (arquivo ou :memory: compartilhado)."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self._is_memory = str(database_path) == ":memory:"
        if self._is_memory:
            self._path_str = ":memory:"
            self._shared_conn: sqlite3.Connection | None = sqlite3.connect(
                ":memory:", check_same_thread=False
            )
        else:
            resolved = Path(database_path).expanduser().resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            self._path_str = str(resolved)
            self._shared_conn = None
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._shared_conn is not None:
                with self._shared_conn:
                    yield self._shared_conn
            else:
                conn = sqlite3.connect(self._path_str, timeout=10.0)
                try:
                    with conn:
                        yield conn
                finally:
                    conn.close()

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS router_tickets (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    repository TEXT NOT NULL,
                    status TEXT NOT NULL,
                    proactive_mode TEXT NOT NULL,
                    workflow_id TEXT,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_router_tickets_status
                    ON router_tickets(status, created_at DESC);

                CREATE TABLE IF NOT EXISTS thread_subscriptions (
                    scope_key TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    thread_kind TEXT NOT NULL,
                    thread_number INTEGER NOT NULL,
                    workflow_id TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_thread_subscriptions_workflow
                    ON thread_subscriptions(workflow_id);

                CREATE TABLE IF NOT EXISTS outbox_messages (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    next_retry_at REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_pending
                    ON outbox_messages(status, next_retry_at);
                """
            )

    @staticmethod
    def _scope_key(
        repository: str,
        thread_kind: Literal["issue", "pull_request"],
        thread_number: int,
    ) -> str:
        return f"{repository.lower()}:{thread_kind}:{thread_number}"

    # ------------------------------------------------------------------ Tickets
    def save_ticket(self, ticket: RouterTicket) -> RouterTicket:
        with self._connect() as db:
            existing = db.execute(
                "SELECT payload FROM router_tickets WHERE event_id = ?",
                (ticket.event_id,),
            ).fetchone()
            if existing is not None:
                return RouterTicket.model_validate_json(existing[0])
            db.execute(
                """
                INSERT INTO router_tickets
                    (id, event_id, repository, status, proactive_mode, workflow_id, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket.id,
                    ticket.event_id,
                    ticket.repository.lower(),
                    ticket.status.value,
                    ticket.proactive_mode.value,
                    ticket.workflow_id,
                    ticket.created_at.isoformat(),
                    ticket.model_dump_json(),
                ),
            )
        return ticket

    def update_ticket(
        self,
        ticket_id: str,
        *,
        status: RouterDecisionStatus | None = None,
        workflow_id: str | None = None,
    ) -> RouterTicket | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM router_tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
            if row is None:
                return None
            current = RouterTicket.model_validate_json(row[0])
            updates: dict[str, Any] = {"updated_at": _utc_now()}
            if status is not None:
                updates["status"] = status
            if workflow_id is not None:
                updates["workflow_id"] = workflow_id
            updated = current.model_copy(update=updates)
            db.execute(
                """
                UPDATE router_tickets
                SET status = ?, workflow_id = ?, payload = ?
                WHERE id = ?
                """,
                (
                    updated.status.value,
                    updated.workflow_id,
                    updated.model_dump_json(),
                    ticket_id,
                ),
            )
            return updated

    def get_ticket(self, ticket_id: str) -> RouterTicket | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM router_tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
        return RouterTicket.model_validate_json(row[0]) if row else None

    def get_ticket_by_event_id(self, event_id: str) -> RouterTicket | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM router_tickets WHERE event_id = ?", (event_id,)
            ).fetchone()
        return RouterTicket.model_validate_json(row[0]) if row else None

    def list_tickets(
        self,
        *,
        status: RouterDecisionStatus | None = None,
        repository: str | None = None,
        limit: int = 50,
    ) -> list[RouterTicket]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if repository is not None:
            clauses.append("repository = ?")
            params.append(repository.lower())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = (
            f"SELECT payload FROM router_tickets {where} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        params.append(max(1, limit))
        with self._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        return [RouterTicket.model_validate_json(row[0]) for row in rows]

    # ------------------------------------------------- Subscriptions (Direct)
    def subscribe_thread(
        self,
        *,
        repository: str,
        thread_kind: Literal["issue", "pull_request"],
        thread_number: int,
        workflow_id: str,
        project_id: str,
        owner_client_id: str,
    ) -> ThreadSubscription:
        scope = self._scope_key(repository, thread_kind, thread_number)
        now = _utc_now()
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM thread_subscriptions WHERE scope_key = ?",
                (scope,),
            ).fetchone()
            if row is not None:
                existing = ThreadSubscription.model_validate_json(row[0])
                sub = existing.model_copy(
                    update={
                        "workflow_id": workflow_id,
                        "project_id": project_id,
                        "owner_client_id": owner_client_id,
                        "active": True,
                        "updated_at": now,
                    }
                )
            else:
                sub = ThreadSubscription(
                    repository=repository,
                    thread_kind=thread_kind,
                    thread_number=thread_number,
                    workflow_id=workflow_id,
                    project_id=project_id,
                    owner_client_id=owner_client_id,
                    active=True,
                    created_at=now,
                    updated_at=now,
                )
            db.execute(
                """
                INSERT OR REPLACE INTO thread_subscriptions
                    (scope_key, repository, thread_kind, thread_number, workflow_id, active, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    repository.lower(),
                    thread_kind,
                    thread_number,
                    workflow_id,
                    1 if sub.active else 0,
                    sub.model_dump_json(),
                ),
            )
            return sub

    def get_active_subscription(
        self,
        repository: str,
        thread_kind: Literal["issue", "pull_request"],
        thread_number: int,
    ) -> ThreadSubscription | None:
        scope = self._scope_key(repository, thread_kind, thread_number)
        with self._connect() as db:
            row = db.execute(
                """
                SELECT payload FROM thread_subscriptions
                WHERE scope_key = ? AND active = 1
                """,
                (scope,),
            ).fetchone()
        return ThreadSubscription.model_validate_json(row[0]) if row else None

    def record_direct_delivery(
        self,
        repository: str,
        thread_kind: Literal["issue", "pull_request"],
        thread_number: int,
        event_id: str,
    ) -> ThreadSubscription | None:
        scope = self._scope_key(repository, thread_kind, thread_number)
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM thread_subscriptions WHERE scope_key = ? AND active = 1",
                (scope,),
            ).fetchone()
            if row is None:
                return None
            current = ThreadSubscription.model_validate_json(row[0])
            updated = current.model_copy(
                update={
                    "direct_deliveries_count": current.direct_deliveries_count + 1,
                    "last_event_id": event_id,
                    "updated_at": _utc_now(),
                }
            )
            db.execute(
                "UPDATE thread_subscriptions SET payload = ? WHERE scope_key = ?",
                (updated.model_dump_json(), scope),
            )
            return updated

    def list_subscriptions(
        self, *, active_only: bool = True, limit: int = 50
    ) -> list[ThreadSubscription]:
        where = "WHERE active = 1" if active_only else ""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT payload FROM thread_subscriptions {where} LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [ThreadSubscription.model_validate_json(row[0]) for row in rows]

    # ------------------------------------------------------------------ Outbox
    def enqueue_outbox(
        self,
        *,
        idempotency_key: str,
        destination_kind: OutboxDestinationKind,
        target: str,
        event_name: str,
        payload: dict[str, Any],
        max_attempts: int = 3,
    ) -> OutboxMessage:
        with self._connect() as db:
            existing = db.execute(
                "SELECT payload FROM outbox_messages WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return OutboxMessage.model_validate_json(existing[0])
            msg = OutboxMessage(
                idempotency_key=idempotency_key,
                destination_kind=destination_kind,
                target=target,
                event_name=event_name,
                payload=payload,
                status=OutboxStatus.PENDING,
                max_attempts=max_attempts,
                next_retry_at=0.0,
            )
            db.execute(
                """
                INSERT INTO outbox_messages
                    (id, idempotency_key, status, next_retry_at, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    msg.id,
                    msg.idempotency_key,
                    msg.status.value,
                    msg.next_retry_at,
                    msg.created_at.isoformat(),
                    msg.model_dump_json(),
                ),
            )
            return msg

    def due_outbox_messages(
        self, *, now: float | None = None, limit: int = 25
    ) -> list[OutboxMessage]:
        current_time = time.time() if now is None else now
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT payload FROM outbox_messages
                WHERE status = ? AND next_retry_at <= ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (OutboxStatus.PENDING.value, current_time, max(1, limit)),
            ).fetchall()
        return [OutboxMessage.model_validate_json(row[0]) for row in rows]

    def mark_outbox_sent(self, message_id: str) -> OutboxMessage | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM outbox_messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                return None
            current = OutboxMessage.model_validate_json(row[0])
            updated = current.model_copy(
                update={
                    "status": OutboxStatus.SENT,
                    "attempt_count": current.attempt_count + 1,
                    "last_error": None,
                    "updated_at": _utc_now(),
                }
            )
            db.execute(
                "UPDATE outbox_messages SET status = ?, payload = ? WHERE id = ?",
                (updated.status.value, updated.model_dump_json(), message_id),
            )
            return updated

    def mark_outbox_failure(
        self,
        message_id: str,
        error: str,
        *,
        base_backoff_seconds: float = 2.0,
        now: float | None = None,
    ) -> OutboxMessage | None:
        current_time = time.time() if now is None else now
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM outbox_messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                return None
            current = OutboxMessage.model_validate_json(row[0])
            attempts = current.attempt_count + 1
            exhausted = attempts >= current.max_attempts
            new_status = OutboxStatus.FAILED if exhausted else OutboxStatus.PENDING
            backoff = base_backoff_seconds * (2 ** max(0, attempts - 1))
            next_retry = current_time + backoff if not exhausted else current.next_retry_at
            updated = current.model_copy(
                update={
                    "status": new_status,
                    "attempt_count": attempts,
                    "next_retry_at": next_retry,
                    "last_error": error[:500],
                    "updated_at": _utc_now(),
                }
            )
            db.execute(
                """
                UPDATE outbox_messages
                SET status = ?, next_retry_at = ?, payload = ?
                WHERE id = ?
                """,
                (
                    updated.status.value,
                    updated.next_retry_at,
                    updated.model_dump_json(),
                    message_id,
                ),
            )
            return updated

    def list_outbox_messages(
        self, *, status: OutboxStatus | None = None, limit: int = 50
    ) -> list[OutboxMessage]:
        if status is not None:
            query = (
                "SELECT payload FROM outbox_messages WHERE status = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            params: tuple[Any, ...] = (status.value, max(1, limit))
        else:
            query = "SELECT payload FROM outbox_messages ORDER BY created_at DESC LIMIT ?"
            params = (max(1, limit),)
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [OutboxMessage.model_validate_json(row[0]) for row in rows]

    # ------------------------------------------------------------ Observability
    def stats(self) -> dict[str, Any]:
        with self._connect() as db:
            ticket_counts = dict(
                db.execute(
                    "SELECT status, COUNT(*) FROM router_tickets GROUP BY status"
                ).fetchall()
            )
            active_subs = db.execute(
                "SELECT COUNT(*) FROM thread_subscriptions WHERE active = 1"
            ).fetchone()[0]
            direct_rows = db.execute(
                "SELECT payload FROM thread_subscriptions"
            ).fetchall()
            direct_deliveries = sum(
                ThreadSubscription.model_validate_json(r[0]).direct_deliveries_count
                for r in direct_rows
            )
            outbox_counts = dict(
                db.execute(
                    "SELECT status, COUNT(*) FROM outbox_messages GROUP BY status"
                ).fetchall()
            )
        return {
            "tickets": {
                "dispatched": ticket_counts.get(
                    RouterDecisionStatus.DISPATCHED.value, 0
                ),
                "observed": ticket_counts.get(RouterDecisionStatus.OBSERVED.value, 0),
                "rejected_fast_path": ticket_counts.get(
                    RouterDecisionStatus.REJECTED_FAST_PATH.value, 0
                ),
                "rejected_slow_path": ticket_counts.get(
                    RouterDecisionStatus.REJECTED_SLOW_PATH.value, 0
                ),
                "direct_subscription": ticket_counts.get(
                    RouterDecisionStatus.DIRECT_SUBSCRIPTION.value, 0
                ),
            },
            "subscriptions": {
                "active_threads": active_subs,
                "direct_deliveries_total": direct_deliveries,
            },
            "outbox": {
                "pending": outbox_counts.get(OutboxStatus.PENDING.value, 0),
                "sent": outbox_counts.get(OutboxStatus.SENT.value, 0),
                "failed": outbox_counts.get(OutboxStatus.FAILED.value, 0),
            },
        }

