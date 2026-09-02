"""Contrato de tarefas do Forgehand.

Resolve os pontos levantados na revisão arquitetural:
- timeout obrigatório por tarefa (regra 4);
- idempotency_key determinística (regra 5);
- budget por tarefa como mecanismo de enforcement da regra 9;
- status ESCALATED explícito — exaurir iterações nunca vira síntese silenciosa.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, computed_field, model_validator


class Capability(str, Enum):
    BACKEND = "backend"
    FRONTEND = "frontend"
    TESTING = "testing"
    REVIEW = "review"
    RESEARCH = "research"
    DOCUMENTATION = "documentation"
    DEVOPS = "devops"
    ARCHITECTURE = "architecture"
    SECURITY = "security"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"  # falha técnica (timeout, exceção, budget estourado)
    BLOCKED = "blocked"  # dependência não satisfeita
    REJECTED = "rejected"  # reprovada pelo judge — elegível a replan
    ESCALATED = "escalated"  # esgotou tentativas — exige decisão humana
    CANCELLED = "cancelled"  # cancelada explicitamente pelo operador

    @property
    def is_terminal(self) -> bool:
        return self in {
            TaskStatus.COMPLETED,
            TaskStatus.ESCALATED,
            TaskStatus.CANCELLED,
        }

    @property
    def is_redispatchable(self) -> bool:
        """Só estes status voltam ao dispatch no replan. COMPLETED nunca re-executa."""
        return self in {TaskStatus.REJECTED, TaskStatus.FAILED, TaskStatus.PENDING}


class TaskBudget(BaseModel):
    """Budget por tarefa. O provider layer DEVE consultar antes de cada chamada."""

    max_tokens: int = Field(default=100_000, gt=0)
    max_cost_usd: float = Field(default=3.00, gt=0)
    consumed_tokens: int = Field(default=0, ge=0)
    consumed_cost_usd: float = Field(default=0.0, ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def exhausted(self) -> bool:
        return (
            self.consumed_tokens >= self.max_tokens
            or self.consumed_cost_usd >= self.max_cost_usd
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def exceeded(self) -> bool:
        """Distingue atingir exatamente o limite de ultrapassá-lo."""
        return (
            self.consumed_tokens > self.max_tokens
            or self.consumed_cost_usd > self.max_cost_usd
        )

    def charge(self, tokens: int, cost_usd: float) -> "TaskBudget":
        """Retorna novo budget (imutável — compatível com reducers do LangGraph)."""
        return self.model_copy(
            update={
                "consumed_tokens": self.consumed_tokens + tokens,
                "consumed_cost_usd": self.consumed_cost_usd + cost_usd,
            }
        )


class TaskAttempt(BaseModel):
    """Registro de cada tentativa — base da rastreabilidade (regra 10)."""

    attempt_number: int = Field(ge=1)
    agent_name: str
    model: str
    started_at: datetime
    finished_at: datetime | None = None
    outcome: TaskStatus | None = None
    failure_reason: str | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0
    trace_id: str | None = None  # correlação com Langfuse
    operational_summary: dict[str, Any] | None = None


class AgentTask(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    title: str
    description: str
    capability: Capability
    dependencies: list[UUID] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)

    # Enforcement das regras arquiteturais
    timeout_seconds: int = Field(default=300, gt=0, le=3600)
    max_attempts: int = Field(default=3, ge=1, le=5)
    budget: TaskBudget = Field(default_factory=TaskBudget)
    is_critical: bool = False  # tarefa crítica → advisor + gate humano em escalation

    # Execução
    assigned_agent: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    # Concedido SOMENTE pelo advisor (regra 8) — o registry seleciona o
    # executor um tier acima; executor nunca escala a si mesmo.
    tier_escalated: bool = False
    # Preenchido quando uma tarefa COMPLETED é reaberta por sinal externo
    # (ex.: "ci:<sha>"). O reducer só aceita rebaixar COMPLETED com um valor
    # novo aqui — evita reabertura acidental por update tardio.
    reopen_reason: str | None = None
    attempts: list[TaskAttempt] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _criteria_before_execution(self) -> "AgentTask":
        """Regra 3: nenhuma tarefa sai de PENDING sem critério de aceitação."""
        if self.status != TaskStatus.PENDING and not self.acceptance_criteria:
            raise ValueError(
                f"Tarefa {self.id} em {self.status} sem acceptance_criteria."
            )
        return self

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    def idempotency_key(self, project_id: str) -> str:
        """Chave determinística por (projeto, tarefa, tentativa).

        Operações externas (git, filesystem, chamadas de API) usam esta chave
        para deduplicação. Retry da MESMA tentativa gera a MESMA chave;
        nova tentativa gera chave nova — retry seguro sem colisão entre attempts.
        """
        raw = f"{project_id}:{self.id}:{self.attempt_count}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def next_status_after_failure(self) -> TaskStatus:
        """Decisão explícita: FAILED/REJECTED até max_attempts, depois ESCALATED."""
        if self.attempt_count >= self.max_attempts:
            return TaskStatus.ESCALATED
        return TaskStatus.REJECTED


class EvaluationResult(BaseModel):
    task_id: UUID
    approved: bool
    score: float = Field(ge=0, le=1)
    criteria_scores: dict[str, float]
    failures: list[str] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)

    # Regra 6: o LLM não é a única fonte de validação.
    # Estes campos são preenchidos por ferramentas objetivas quando disponíveis
    # (Fase 1: podem ser None; o judge declara o que conseguiu validar).
    tests_passed: bool | None = None
    lint_passed: bool | None = None
    type_check_passed: bool | None = None
    validated_by: list[str] = Field(
        default_factory=lambda: ["llm"],
        description="Fontes que participaram: llm, pytest, ruff, mypy, sandbox",
    )

    @model_validator(mode="after")
    def _objective_signals_veto(self) -> "EvaluationResult":
        """Sinal objetivo reprova mesmo que o LLM aprove — nunca o contrário."""
        objective_failures = [
            s
            for s in (self.tests_passed, self.lint_passed, self.type_check_passed)
            if s is False
        ]
        if objective_failures and self.approved:
            raise ValueError(
                "approved=True com sinal objetivo falhando. "
                "O LLM não pode sobrescrever pytest/lint/mypy."
            )
        return self


class AdvisorTrigger(BaseModel):
    """Sinais OBJETIVOS para escalonamento. Confidence é secundária."""

    task: AgentTask
    judge_rejections: int = 0
    repeated_diff_detected: bool = False  # mesma solução em tentativas seguidas
    self_reported_confidence: float | None = Field(default=None, ge=0, le=1)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def should_consult(self) -> bool:
        return (
            self.task.is_critical
            or self.task.attempt_count >= 2
            or self.judge_rejections >= 1
            or self.repeated_diff_detected
            or self.task.budget.exhausted
            or (
                self.self_reported_confidence is not None
                and self.self_reported_confidence < 0.5  # só como último sinal
            )
        )
