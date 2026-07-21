from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from pathlib import Path


@dataclass(frozen=True)
class AuditEvent:
    timestamp: str
    action: str
    outcome: str
    client_id: str | None = None
    project_id: str | None = None
    workflow_id: str | None = None
    request_path: str | None = None
    request_method: str | None = None
    status_code: int | None = None
    detail: str | None = None
    remote_addr: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class InMemoryAuditLog:
    def __init__(self, max_events: int = 500) -> None:
        self._events: deque[AuditEvent] = deque(maxlen=max_events)

    async def record(self, event: AuditEvent) -> None:
        self._events.append(event)

    async def list_recent(
        self, limit: int = 50, client_id: str | None = None
    ) -> list[AuditEvent]:
        events = list(self._events)
        if client_id is not None:
            events = [event for event in events if event.client_id in {None, client_id}]
        return list(reversed(events[-limit:]))

    async def metrics(self) -> dict[str, int]:
        return {
            "stored_events": len(self._events),
            "capacity": self._events.maxlen or 0,
        }


class JsonlAuditLog:
    """Audit log append-only persistente para instalações single-node."""

    def __init__(self, path: str, max_events: int = 10_000) -> None:
        self._path = Path(path).expanduser().resolve()
        self._max_events = max_events
        self._lock = asyncio.Lock()

    async def record(self, event: AuditEvent) -> None:
        async with self._lock:
            await asyncio.to_thread(self._append, event)

    def _append(self, event: AuditEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    async def list_recent(
        self, limit: int = 50, client_id: str | None = None
    ) -> list[AuditEvent]:
        async with self._lock:
            events = await asyncio.to_thread(self._read_events)
        if client_id is not None:
            events = [event for event in events if event.client_id in {None, client_id}]
        return list(reversed(events[-limit:]))

    def _read_events(self) -> list[AuditEvent]:
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        events = [AuditEvent(**json.loads(line)) for line in lines[-self._max_events :]]
        if len(lines) > self._max_events:
            self._path.write_text(
                "\n".join(
                    json.dumps(event.to_dict(), ensure_ascii=False) for event in events
                )
                + "\n",
                encoding="utf-8",
            )
        return events

    async def metrics(self) -> dict[str, int]:
        events = await self.list_recent(limit=self._max_events)
        return {"stored_events": len(events), "capacity": self._max_events}


def build_audit_event(
    *,
    action: str,
    outcome: str,
    client_id: str | None = None,
    project_id: str | None = None,
    workflow_id: str | None = None,
    request_path: str | None = None,
    request_method: str | None = None,
    status_code: int | None = None,
    detail: str | None = None,
    remote_addr: str | None = None,
) -> AuditEvent:
    return AuditEvent(
        timestamp=datetime.now(timezone.utc).isoformat(),
        action=action,
        outcome=outcome,
        client_id=client_id,
        project_id=project_id,
        workflow_id=workflow_id,
        request_path=request_path,
        request_method=request_method,
        status_code=status_code,
        detail=detail,
        remote_addr=remote_addr,
    )
