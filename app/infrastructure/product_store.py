"""Single-host SQLite state machine. No user/model paths are used on disk."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


class ProductConflict(ValueError):
    pass


class ProductStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, project TEXT NOT NULL,
                key TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                updated REAL NOT NULL, payload TEXT NOT NULL,
                UNIQUE(owner, project, key))""")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, owner: str, idea: dict[str, Any], fingerprint: str) -> tuple[dict[str, Any], bool]:
        product = {**idea, "id": str(uuid4()), "status": "drafting", "brief": None,
                   "app": None, "cost_usd": 0.0, "reserved_usd": 0.0, "tokens": 0,
                   "error": None}
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM products WHERE owner=? AND project=? AND key=?",
                             (owner, idea["project_id"], idea["idempotency_key"])).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise ProductConflict("Chave já utilizada com outra ideia.")
                return json.loads(old["payload"]), False
            db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?)",
                       (product["id"], owner, idea["project_id"], idea["idempotency_key"],
                        fingerprint, "drafting", time.time(), json.dumps(product)))
        return product, True

    def get(self, product_id: str, owner: str) -> dict[str, Any]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM products WHERE id=? AND owner=?", (product_id, owner)).fetchone()
            if row is None:
                raise KeyError(product_id)
            product: dict[str, Any] = json.loads(row["payload"])
            if row["state"] in {"drafting", "building"} and time.time() - row["updated"] > 300:
                product.update(status="failed", error="operation_interrupted")
                db.execute("UPDATE products SET state=?, payload=? WHERE id=?",
                           ("failed", json.dumps(product), product_id))
            return product

    def list(self, owner: str, project: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            ids = [row["id"] for row in db.execute(
                "SELECT id FROM products WHERE owner=? AND project=? ORDER BY updated DESC LIMIT 50",
                (owner, project))]
        return [self.get(product_id, owner) for product_id in ids]

    def transition(self, product: dict[str, Any], expected: str) -> bool:
        with self.connection() as db:
            changed = db.execute(
                "UPDATE products SET state=?, updated=?, payload=? WHERE id=? AND state=?",
                (product["status"], time.time(), json.dumps(product), product["id"], expected))
            return changed.rowcount == 1
