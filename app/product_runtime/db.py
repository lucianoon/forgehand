"""Small, parameterized SQL boundary for PostgreSQL and local SQLite."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Any, Iterator

SCHEMA_VERSION = 1
DDL = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, model_hash TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires BIGINT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires)",
    "CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), entity TEXT NOT NULL, payload TEXT NOT NULL, search_text TEXT NOT NULL, version INTEGER NOT NULL, created BIGINT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS records_owner_entity ON records(user_id, entity, created, id)",
    "CREATE TABLE IF NOT EXISTS login_attempts (bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires BIGINT NOT NULL)",
)


class Connection:
    def __init__(self, connection: Any, sqlite: bool):
        self.raw, self.sqlite = connection, sqlite

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        # SQL is exclusively versioned source, never supplied by users/models.
        return self.raw.execute(sql.replace("%s", "?") if self.sqlite else sql, params)


class Database:
    def __init__(self, url: str):
        self.sqlite = url.startswith("sqlite:///")
        self.pool: Any = None
        self.path = url.removeprefix("sqlite:///")
        if self.sqlite:
            if self.path == ":memory:":
                raise ValueError("Use a file database so connections share durable state")
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        elif url.startswith(("postgresql://", "postgres://")):
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
            self.pool = ConnectionPool(url, min_size=1, max_size=8, timeout=5,
                                       kwargs={"row_factory": dict_row}, open=True)
        else:
            raise ValueError("Use PostgreSQL or sqlite:/// for local development")

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        if self.sqlite:
            raw = sqlite3.connect(self.path, timeout=5)
            raw.row_factory = sqlite3.Row
            raw.execute("PRAGMA foreign_keys=ON")
            try:
                with raw:
                    raw.execute("BEGIN IMMEDIATE")
                    yield Connection(raw, True)
            finally:
                raw.close()
        else:
            with self.pool.connection() as raw:
                yield Connection(raw, False)

    def migrate(self, model_hash: str) -> None:
        with self.connection() as db:
            if not self.sqlite:
                db.execute("SELECT pg_advisory_xact_lock(74832017)")
            # Bootstrap metadata first; never apply DDL to an incompatible version.
            db.execute(DDL[0])
            rows = db.execute("SELECT version, model_hash FROM schema_version").fetchall()
            if rows:
                self._compatible(rows, model_hash)
                return
            for statement in DDL[1:]:
                db.execute(statement)
            db.execute("INSERT INTO schema_version VALUES (%s,%s)", (SCHEMA_VERSION, model_hash))

    @staticmethod
    def _compatible(rows: Any, model_hash: str) -> None:
        if len(rows) != 1 or rows[0]["version"] != SCHEMA_VERSION or rows[0]["model_hash"] != model_hash:
            raise ValueError("Database schema/model mismatch; an explicit migration is required")

    def ready(self, model_hash: str) -> None:
        with self.connection() as db:
            rows = db.execute("SELECT version, model_hash FROM schema_version").fetchall()
            self._compatible(rows, model_hash)

    def close(self) -> None:
        if self.pool is not None:
            self.pool.close()
