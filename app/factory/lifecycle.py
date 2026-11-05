"""Durable local resource inventory, outside the untrusted checkout."""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from app.infrastructure.posix import flock_exclusive_nonblocking
from app.models.factory import WorkspaceLease

_lock_fd: ContextVar[int | None] = ContextVar("factory_lock_fd", default=None)
_lock_owner: ContextVar[str | None] = ContextVar("factory_lock_owner", default=None)


def inherited_lock_fds() -> tuple[int, ...]:
    fd = _lock_fd.get()
    inherited = [] if fd is None else [fd]
    maintenance_fd = os.environ.get("FORGEHAND_MAINTENANCE_FD")
    maintenance_path = os.environ.get("FORGEHAND_MAINTENANCE_LOCK_PATH")
    if maintenance_fd is not None or maintenance_path is not None:
        try:
            if maintenance_fd is None or maintenance_path is None:
                raise ValueError
            number = int(maintenance_fd)
            if number < 3 or not Path(maintenance_path).is_absolute():
                raise ValueError
            descriptor = os.fstat(number)
            path = os.lstat(maintenance_path)
            if (
                not stat.S_ISREG(descriptor.st_mode)
                or not stat.S_ISREG(path.st_mode)
                or (descriptor.st_dev, descriptor.st_ino) != (path.st_dev, path.st_ino)
            ):
                raise ValueError
        except (ValueError, OSError):
            raise ValueError("installation_maintenance_lock_invalid") from None
        if number not in inherited:
            inherited.append(number)
    return tuple(inherited)


class WorkspaceBusy(RuntimeError):
    pass


class WorkspaceJournal:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "lifecycle.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS leases (workflow TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, workflow TEXT NOT NULL, state TEXT NOT NULL, timestamp TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS containers (workflow TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS phases (workflow TEXT PRIMARY KEY, phase TEXT NOT NULL, outcome TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, lease: WorkspaceLease) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO leases VALUES (?, ?)",
                (lease.workflow_id, lease.model_dump_json()),
            )
            db.execute(
                "INSERT INTO events(workflow,state,timestamp) VALUES (?,?,?)",
                (lease.workflow_id, lease.state.value, lease.updated_at.isoformat()),
            )

    def get(self, workflow_id: str) -> WorkspaceLease | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM leases WHERE workflow=?", (workflow_id,)
            ).fetchone()
        return WorkspaceLease.model_validate_json(row[0]) if row else None

    def leases(self) -> list[WorkspaceLease]:
        with self.connect() as db:
            return [
                WorkspaceLease.model_validate_json(row[0])
                for row in db.execute("SELECT payload FROM leases")
            ]

    def history(self, workflow_id: str) -> list[dict[str, str]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT state,timestamp FROM events WHERE workflow=? ORDER BY id DESC LIMIT 50",
                (workflow_id,),
            ).fetchall()
        return [{"state": row[0], "timestamp": row[1]} for row in reversed(rows)]

    def record_container(self, workflow_id: str, name: str, token: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO containers VALUES (?,?,?)",
                (workflow_id, name, token),
            )

    def containers(self) -> dict[str, tuple[str, str]]:
        with self.connect() as db:
            return {
                row[0]: (row[1], row[2])
                for row in db.execute("SELECT workflow,name,token FROM containers")
            }

    def forget_container(self, workflow_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM containers WHERE workflow=?", (workflow_id,))

    def record_phase(self, workflow_id: str, phase: str, outcome: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO phases VALUES (?,?,?)",
                (workflow_id, phase, outcome),
            )

    def phase(self, workflow_id: str) -> dict[str, str] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT phase,outcome FROM phases WHERE workflow=?", (workflow_id,)
            ).fetchone()
        return {"phase": row[0], "outcome": row[1]} if row else None

    @contextmanager
    def exclusive(self, workflow_id: str, *, reentrant: bool = False) -> Iterator[None]:
        # Hash avoids filesystem names supplied by clients. Local children
        # inherit this fd, so cleanup cannot race a surviving Git process.
        import hashlib

        owner_key = f"{self.root}:{workflow_id}"
        if reentrant and _lock_owner.get() == owner_key:
            yield
            return

        name = hashlib.sha256(workflow_id.encode()).hexdigest()
        with (self.root / f"{name}.lock").open("a") as handle:
            try:
                flock_exclusive_nonblocking(handle.fileno())
            except BlockingIOError:
                raise WorkspaceBusy(workflow_id) from None
            token = _lock_fd.set(handle.fileno())
            owner_token = _lock_owner.set(owner_key)
            try:
                yield
            finally:
                _lock_fd.reset(token)
                _lock_owner.reset(owner_token)
                # Close, not LOCK_UN: inherited descriptors retain the lock.
