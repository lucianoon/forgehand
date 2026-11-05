"""Modelos de domínio da camada Super-Agent (Router, Subscriptions, Outbox e Proatividade).

Implementa os conceitos de engenharia inspirados no dsh-superagent-prototype:
- Filtragem em dois níveis (Fast Path programático + Slow Path semântico via LLM leve)
- Modos de trabalho proativo (off, observe, auto)
- Subscription Direct (vínculo direto Thread/PR <-> Workflow sem reavaliar no Router)
- Fila Outbox persistente com idempotência e retry para efeitos externos
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProactiveMode(str, Enum):
    OFF = "off"
    OBSERVE = "observe"
    AUTO = "auto"


class IngressEventKind(str, Enum):
    ISSUE_OPENED = "issue_opened"
    ISSUE_COMMENT = "issue_comment"
    PR_REVIEW = "pr_review"
    PR_REVIEW_COMMENT = "pr_review_comment"


class RouterDecisionStatus(str, Enum):
    DISPATCHED = "dispatched"
    OBSERVED = "observed"
    REJECTED_FAST_PATH = "rejected_fast_path"
    REJECTED_SLOW_PATH = "rejected_slow_path"
    DIRECT_SUBSCRIPTION = "direct_subscription"


class OutboxDestinationKind(str, Enum):
    WEBHOOK = "webhook"
    GITHUB_COMMENT = "github_comment"


class OutboxStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class RouterEvaluation(BaseModel):
    """Schema estruturado devolvido pelo modelo leve (Slow Path) do Router."""

    model_config = ConfigDict(extra="forbid")

    should_act: bool
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=5, max_length=500)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=10)
    reasoning: str = Field(min_length=5, max_length=1000)


class IngressEvent(BaseModel):
    """Evento externo normalizado antes de passar pelo Router."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    kind: IngressEventKind
    repository: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    scm_host: str = Field(default="github.com", min_length=1)
    sender: str = Field(min_length=1)
    sender_is_bot: bool = False
    thread_kind: Literal["issue", "pull_request"] = "issue"
    thread_number: int = Field(gt=0)
    title: str = ""
    body: str = ""
    html_url: str = ""
    base_ref: str = "main"
    explicit_mention: bool = False
    created_at: datetime = Field(default_factory=_utc_now)


class RouterTicket(BaseModel):
    """Registro persistido de triagem do Router (incluindo candidatos em modo observe)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    event_id: str = Field(min_length=1)
    repository: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    scm_host: str = "github.com"
    base_ref: str = "main"
    thread_kind: Literal["issue", "pull_request"] = "issue"
    thread_number: int = Field(gt=0)
    sender: str = Field(min_length=1)
    title: str = ""
    summary: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(default_factory=list)
    proactive_mode: ProactiveMode = ProactiveMode.OFF
    status: RouterDecisionStatus
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reasoning: str = ""
    workflow_id: str | None = None
    source_url: str = ""
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class ThreadSubscription(BaseModel):
    """Assinatura de Thread (Subscription Direct): eventos futuros vão direto ao workflow."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    repository: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    thread_kind: Literal["issue", "pull_request"] = "issue"
    thread_number: int = Field(gt=0)
    workflow_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    owner_client_id: str = Field(min_length=1)
    active: bool = True
    direct_deliveries_count: int = Field(default=0, ge=0)
    last_event_id: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class OutboxMessage(BaseModel):
    """Mensagem na fila Outbox para entrega assíncrona idempotente com retry."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    idempotency_key: str = Field(min_length=1)
    destination_kind: OutboxDestinationKind
    target: str = Field(min_length=1)
    event_name: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    status: OutboxStatus = OutboxStatus.PENDING
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    next_retry_at: float = 0.0
    last_error: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

