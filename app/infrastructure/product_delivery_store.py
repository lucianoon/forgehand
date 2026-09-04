"""Additive single-host delivery ledger; no transcript/credential storage."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any
from uuid import uuid4

from app.infrastructure.product_store import ProductConflict, ProductStore
from app.models.product_delivery import AppendDelivery, ProductDeliveryPlan


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def feature_rows(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**f, "id": str(uuid4()), "status": "pending", "attempts": []} for f in features
    ]


def next_feature(plan: dict[str, Any]) -> dict[str, Any] | None:
    return next((f for f in plan["features"] if f["status"] != "merged"), None)


class ProductDeliveryStore:
    def __init__(self, products: ProductStore):
        self.products = products
        with products.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS product_delivery_plans (
                product_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                revision INTEGER NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS product_delivery_attempts (
                workflow_id TEXT PRIMARY KEY, product_id TEXT NOT NULL,
                owner TEXT NOT NULL, context TEXT NOT NULL, work_order TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS product_delivery_dispatch_intents (
                workflow_id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
                work_order_digest TEXT NOT NULL)""")

    def _load(
        self, db: sqlite3.Connection, product_id: str, owner: str
    ) -> dict[str, Any]:
        row = db.execute(
            "SELECT payload FROM product_delivery_plans WHERE product_id=? AND owner=?",
            (product_id, owner),
        ).fetchone()
        if row is None:
            raise KeyError(product_id)
        result: dict[str, Any] = json.loads(row["payload"])
        return result

    def get(self, product_id: str, owner: str) -> dict[str, Any]:
        with self.products.connection() as db:
            return self._load(db, product_id, owner)

    def create(
        self, product: dict[str, Any], owner: str, spec: ProductDeliveryPlan
    ) -> dict[str, Any]:
        if product["status"] != "ready_for_preview":
            raise ProductConflict("Gere e aprove o produto antes de planejar entregas.")
        fingerprint = hashlib.sha256(encoded(spec.model_dump()).encode()).hexdigest()
        plan = {
            **spec.model_dump(),
            "product_id": product["id"],
            "project_id": product["project_id"],
            "revision": 1,
            "original_brief": product["brief"],
            "features": feature_rows([f.model_dump() for f in spec.features]),
        }
        with self.products.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT owner, fingerprint FROM product_delivery_plans WHERE product_id=?",
                (product["id"],),
            ).fetchone()
            if row:
                if row["owner"] != owner or row["fingerprint"] != fingerprint:
                    raise ProductConflict(
                        "Plano já existe. Use acrescentar; destino e critérios são preservados."
                    )
                return self._load(db, product["id"], owner)
            db.execute(
                "INSERT INTO product_delivery_plans VALUES(?,?,?,?,?)",
                (product["id"], owner, 1, fingerprint, encoded(plan)),
            )
        return plan

    @staticmethod
    def _revision(plan: dict[str, Any], revision: int) -> None:
        if plan["revision"] != revision:
            raise ProductConflict(
                "Plano atualizado por outra operação. Atualize antes de continuar."
            )

    @staticmethod
    def _save(db: sqlite3.Connection, plan: dict[str, Any], owner: str) -> None:
        plan["revision"] += 1
        db.execute(
            "UPDATE product_delivery_plans SET revision=?, payload=? WHERE product_id=? AND owner=?",
            (plan["revision"], encoded(plan), plan["product_id"], owner),
        )

    def append(
        self, product_id: str, owner: str, body: AppendDelivery
    ) -> dict[str, Any]:
        with self.products.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._load(db, product_id, owner)
            self._revision(plan, body.revision)
            current = next_feature(plan)
            if current and current["status"] not in {"pending", "failed", "cancelled"}:
                raise ProductConflict(
                    "Conclua a entrega ativa antes de alterar o contexto."
                )
            if (
                len(plan["features"]) + len(body.features) > 20
                or len(plan["decisions"]) + len(body.decisions) > 20
            ):
                raise ProductConflict(
                    "Limite de 20 entregas e 20 decisões por produto."
                )
            plan["features"].extend(
                feature_rows([f.model_dump() for f in body.features])
            )
            plan["decisions"].extend(body.decisions)
            self._save(db, plan, owner)
            return plan

    def reserve(
        self,
        product_id: str,
        owner: str,
        revision: int,
        workflow_id: str,
        context: str,
        work_order: dict[str, Any],
        dispatch_scope: str | None = None,
    ) -> dict[str, Any]:
        with self.products.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._load(db, product_id, owner)
            self._revision(plan, revision)
            feature = next_feature(plan)
            if not feature or feature["status"] not in {
                "pending",
                "failed",
                "cancelled",
            }:
                raise ProductConflict(
                    "Há entrega pendente de revisão ou execução; reconcilie antes de continuar."
                )
            if len(feature["attempts"]) >= 3:
                raise ProductConflict("Limite de três tentativas por entrega atingido.")
            feature["status"] = "dispatching"
            feature["attempts"].append(
                {
                    "workflow_id": workflow_id,
                    "status": "dispatching",
                    "context_sha256": hashlib.sha256(context.encode()).hexdigest(),
                    "budget": work_order["limits"],
                    "receipt": None,
                }
            )
            db.execute(
                "INSERT INTO product_delivery_attempts VALUES(?,?,?,?,?)",
                (workflow_id, product_id, owner, context, encoded(work_order)),
            )
            if dispatch_scope is not None:
                db.execute(
                    "INSERT INTO product_delivery_dispatch_intents VALUES(?,?,?)",
                    (workflow_id, dispatch_scope,
                     hashlib.sha256(encoded(work_order).encode()).hexdigest()),
                )
            self._save(db, plan, owner)
            return plan

    def dispatch_intent(
        self, product_id: str, owner: str, workflow_id: str
    ) -> tuple[dict[str, Any], str]:
        with self.products.connection() as db:
            row = db.execute("""
                SELECT a.work_order, i.namespace, i.work_order_digest
                FROM product_delivery_attempts a
                JOIN product_delivery_dispatch_intents i USING (workflow_id)
                WHERE a.product_id=? AND a.owner=? AND a.workflow_id=?
            """, (product_id, owner, workflow_id)).fetchone()
            if row is None:
                raise ProductConflict("Tentativa legada sem intenção recuperável; investigue operacionalmente.")
            order: dict[str, Any] = json.loads(row["work_order"])
            if hashlib.sha256(encoded(order).encode()).hexdigest() != row["work_order_digest"]:
                raise ProductConflict("Integridade da ordem salva não confirmada; recuperação bloqueada.")
            return order, str(row["namespace"])

    def update_attempt(
        self,
        product_id: str,
        owner: str,
        revision: int,
        status: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        with self.products.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._load(db, product_id, owner)
            # Reconciliation arriving late must not regress a newer receipt.
            if plan["revision"] != revision:
                return plan
            feature = next_feature(plan)
            if feature is None or not feature["attempts"]:
                return plan
            attempt = feature["attempts"][-1]
            if feature["status"] == status and all(
                attempt.get(k) == v for k, v in evidence.items()
            ):
                return plan
            feature["status"] = status
            attempt.update(status=status, **evidence)
            self._save(db, plan, owner)
            return plan

    def context(self, product_id: str, owner: str, workflow_id: str) -> dict[str, Any]:
        with self.products.connection() as db:
            row = db.execute(
                "SELECT context FROM product_delivery_attempts WHERE product_id=? AND owner=? AND workflow_id=?",
                (product_id, owner, workflow_id),
            ).fetchone()
            if row is None:
                raise KeyError(workflow_id)
            return {
                "context": json.loads(row["context"]),
                "sha256": hashlib.sha256(row["context"].encode()).hexdigest(),
            }
