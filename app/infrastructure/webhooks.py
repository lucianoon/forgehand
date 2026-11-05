from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

from app.models.superagent import OutboxDestinationKind, OutboxMessage

if TYPE_CHECKING:
    from app.infrastructure.scm import GitHubSCMClient
    from app.infrastructure.superagent_store import SuperAgentStore


class WebhookDispatcher:
    """Despachante unificado de eventos externos e fila Outbox persistente.

    Quando `store` (SuperAgentStore) é fornecido:
    - Todas as notificações (webhooks e comentários no GitHub) entram na Outbox
      com `idempotency_key`, garantindo idempotência e retry com backoff exponencial.
    - Quando usado sem `store`, mantém compatibilidade direta em memória.
    """

    def __init__(
        self,
        urls: list[str],
        signing_secret: str,
        *,
        client: httpx.AsyncClient | None = None,
        store: SuperAgentStore | None = None,
        scm_client_factory: Callable[[], GitHubSCMClient | None] | None = None,
        max_attempts: int = 3,
        base_backoff_seconds: float = 2.0,
    ) -> None:
        self._urls = urls
        self._secret = signing_secret.encode()
        self._client = client or httpx.AsyncClient(timeout=10)
        self._store = store
        self._scm_client_factory = scm_client_factory
        self._max_attempts = max_attempts
        self._base_backoff_seconds = base_backoff_seconds

    @property
    def store(self) -> SuperAgentStore | None:
        return self._store

    async def publish(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> None:
        if not self._urls:
            return
        if self._store is not None:
            base_key = idempotency_key or str(uuid.uuid4())
            for url in self._urls:
                url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
                self._store.enqueue_outbox(
                    idempotency_key=f"webhook:{event}:{base_key}:{url_hash}",
                    destination_kind=OutboxDestinationKind.WEBHOOK,
                    target=url,
                    event_name=event,
                    payload=payload,
                    max_attempts=self._max_attempts,
                )
            await self.flush_outbox()
            return

        body = self._encode_webhook_body(event, payload)
        signature = self._sign(body)
        await asyncio.gather(
            *(
                self._client.post(
                    url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Forgehand-Event": event,
                        "X-Forgehand-Signature-256": f"sha256={signature}",
                    },
                )
                for url in self._urls
            ),
            return_exceptions=True,
        )

    async def enqueue_github_comment(
        self,
        *,
        repository: str,
        thread_number: int,
        body: str,
        idempotency_key: str,
        event_name: str = "github.comment",
        immediate: bool = True,
    ) -> OutboxMessage:
        if self._store is None:
            raise RuntimeError("SuperAgentStore não configurado para Outbox.")
        target = f"{repository}#{thread_number}"
        msg = self._store.enqueue_outbox(
            idempotency_key=f"gh_comment:{idempotency_key}",
            destination_kind=OutboxDestinationKind.GITHUB_COMMENT,
            target=target,
            event_name=event_name,
            payload={
                "repository": repository,
                "thread_number": thread_number,
                "body": body,
            },
            max_attempts=self._max_attempts,
        )
        if immediate:
            await self.flush_outbox()
            refreshed = [
                m
                for m in self._store.list_outbox_messages(limit=20)
                if m.id == msg.id
            ]
            if refreshed:
                return refreshed[0]
        return msg

    async def flush_outbox(self, *, now: float | None = None) -> dict[str, int]:
        """Processa mensagens pendentes na fila Outbox com retry e backoff."""
        if self._store is None:
            return {"processed": 0, "sent": 0, "failed_attempt": 0}
        due = self._store.due_outbox_messages(now=now)
        sent = 0
        failed_attempt = 0
        for msg in due:
            try:
                await self._deliver_message(msg)
            except Exception as exc:  # noqa: BLE001
                failed_attempt += 1
                self._store.mark_outbox_failure(
                    msg.id,
                    f"{type(exc).__name__}: {exc}",
                    base_backoff_seconds=self._base_backoff_seconds,
                    now=now,
                )
            else:
                sent += 1
                self._store.mark_outbox_sent(msg.id)
        return {
            "processed": len(due),
            "sent": sent,
            "failed_attempt": failed_attempt,
        }

    async def _deliver_message(self, msg: OutboxMessage) -> None:
        if msg.destination_kind == OutboxDestinationKind.WEBHOOK:
            body = self._encode_webhook_body(msg.event_name, msg.payload)
            signature = self._sign(body)
            response = await self._client.post(
                msg.target,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Forgehand-Event": msg.event_name,
                    "X-Forgehand-Signature-256": f"sha256={signature}",
                },
            )
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}")
            return

        if msg.destination_kind == OutboxDestinationKind.GITHUB_COMMENT:
            if self._scm_client_factory is None:
                raise RuntimeError("scm_client_factory não configurado")
            scm = self._scm_client_factory()
            if scm is None:
                raise RuntimeError("Credencial GitHub não disponível")
            try:
                await scm.post_issue_comment(
                    repository=str(msg.payload["repository"]),
                    issue_number=int(msg.payload["thread_number"]),
                    body=str(msg.payload["body"]),
                )
            finally:
                await scm.close()
            return

    @staticmethod
    def _encode_webhook_body(event: str, payload: dict[str, Any]) -> bytes:
        return json.dumps(
            {"event": event, "payload": payload},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()

    def _sign(self, body: bytes) -> str:
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    async def close(self) -> None:
        await self._client.aclose()
