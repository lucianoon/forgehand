"""Estado compartilhado do workflow — projetado para fan-out paralelo.

Decisões estruturais:

1. FONTE ÚNICA DE VERDADE: só existe `plan`. Não há completed_tasks nem
   failed_tasks — são views derivadas por filtro de status. Elimina a
   dessincronização apontada na revisão.

2. REDUCER POR MERGE DE ID: quando dispatch_tasks fizer fan-out via Send(),
   cada branch retorna {"plan": [task_atualizada]} contendo APENAS as tarefas
   que tocou. O reducer faz upsert por id — escritas concorrentes de branches
   diferentes nunca colidem porque tocam tarefas diferentes.

3. BUDGET GLOBAL COM REDUCER ADITIVO: branches paralelas somam consumo sem
   race condition. O circuit breaker lê o total consolidado.
"""

from __future__ import annotations

import operator
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, Field

from app.models.factory import (
    BuildProfileSelection,
    FactoryStage,
    WorkOrder,
    WorkspaceLease,
)
from app.models.task import AgentTask, EvaluationResult, TaskStatus


# --------------------------------------------------------------------------
# Reducers
# --------------------------------------------------------------------------


def merge_tasks_by_id(
    existing: list[AgentTask], incoming: list[AgentTask]
) -> list[AgentTask]:
    """Upsert por task.id preservando a ordem do plano.

    Regras de merge quando a mesma tarefa aparece nos dois lados:
    - COMPLETED nunca é rebaixado;
    - ESCALATED só pode voltar para REJECTED por retry humano explícito ou
      CANCELLED por cancelamento explícito do workflow;
    - fora isso, vence a versão com updated_at mais recente.
    """
    merged: dict[str, AgentTask] = {str(t.id): t for t in existing}
    for task in incoming:
        key = str(task.id)
        current = merged.get(key)
        if current is None:
            merged[key] = task
            continue
        if (
            current.status == TaskStatus.COMPLETED
            and task.status != TaskStatus.COMPLETED
        ):
            # Tarefa aprovada não é reaberta por update tardio — a exceção é
            # a reabertura EXPLÍCITA por sinal externo (CI reprovou o commit
            # publicado), marcada com um reopen_reason novo.
            if task.reopen_reason and task.reopen_reason != current.reopen_reason:
                merged[key] = task
            continue
        if current.status == TaskStatus.ESCALATED and task.status not in {
            TaskStatus.ESCALATED,
            TaskStatus.REJECTED,
            TaskStatus.CANCELLED,
        }:
            continue  # só o retry humano reabre uma tarefa escalada
        if task.updated_at >= current.updated_at:
            merged[key] = task
    return list(merged.values())


def keep_latest(existing: Any, incoming: Any) -> Any:
    """Last-write-wins para campos escalares atualizados por um nó por vez.

    O LangGraph só invoca o reducer quando um nó ESCREVEU a chave — logo,
    None entrante é sempre escrita intencional (ex.: authorize_retry limpa
    human_decision após consumir a decisão). Descartar None aqui silenciaria
    exatamente essa intenção; quem não quer alterar o campo simplesmente
    não retorna a chave."""
    return incoming


# --------------------------------------------------------------------------
# Budget global do workflow (regra 9 com enforcement)
# --------------------------------------------------------------------------


class WorkflowBudget(BaseModel):
    max_tokens: int = Field(default=500_000, gt=0)
    max_cost_usd: float = Field(default=5.00, gt=0)
    max_iterations: int = Field(default=3, ge=1)
    max_wall_clock_seconds: int = Field(default=1800, gt=0)


def add_usage(
    existing: dict[str, float], incoming: dict[str, float]
) -> dict[str, float]:
    """Reducer aditivo: branches paralelas somam {tokens, cost_usd} sem colisão."""
    return {
        "tokens": existing.get("tokens", 0) + incoming.get("tokens", 0),
        "cost_usd": existing.get("cost_usd", 0.0) + incoming.get("cost_usd", 0.0),
    }


class WorkflowPhase(str, Enum):
    QUEUED = "queued"
    LOADING_CONTEXT = "loading_context"
    UNSUPPORTED_BUILD_STRATEGY = "unsupported_build_strategy"
    PLANNING = "planning"
    EXECUTING = "executing"
    EVALUATING = "evaluating"
    REPLANNING = "replanning"
    AWAITING_HUMAN = "awaiting_human"  # interrupt() — gate humano explícito
    SYNTHESIZING = "synthesizing"
    DELIVERING = "delivering"  # publicando PR e aguardando CI
    PERSISTING = "persisting"
    COMPLETED = "completed"
    READY_FOR_HUMAN_REVIEW = "ready_for_human_review"
    FAILED = "failed"
    CANCELLED = "cancelled"


# --------------------------------------------------------------------------
# Entrega (PR + CI) — o último trecho do ciclo
# --------------------------------------------------------------------------


class DeliveryConfig(BaseModel):
    """Para onde a entrega vai. Definido na criação do workflow; imutável."""

    repository: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    base_branch: str = Field(default="main", min_length=1)
    head_branch: str | None = Field(default=None, min_length=1)
    pinned_base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    expected_head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    title: str | None = None
    wait_for_checks: bool = True
    checks_timeout_seconds: int = Field(default=900, ge=30, le=7200)


class DeliveryResult(BaseModel):
    """O que aconteceu na última publicação. ci_state:
    success | failure | pending (timeout) | none (repo sem CI) |
    skipped (não esperou / nada a publicar) | error (falha ao publicar)."""

    pull_request_number: int | None = None
    url: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    ci_state: str = "skipped"
    checks: list[dict[str, Any]] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    failure_paths: list[str] = Field(default_factory=list)
    files: int = 0
    deletions: int = 0
    attempts: int = 0
    error: str | None = None
    note: str | None = None

    @property
    def ci_failed(self) -> bool:
        return self.ci_state == "failure"


# --------------------------------------------------------------------------
# Estado
# --------------------------------------------------------------------------


class WorkflowState(BaseModel):
    """Pydantic em vez de TypedDict: validação na fronteira de cada nó
    e serialização direta para o checkpointer PostgreSQL."""

    # Imutáveis após criação
    request: str
    project_id: str
    workflow_id: str
    owner_client_id: str
    budget: WorkflowBudget = Field(default_factory=WorkflowBudget)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    delivery: DeliveryConfig | None = None
    work_order: WorkOrder | None = None

    # Fonte única de verdade das tarefas — reducer de merge por id
    plan: Annotated[list[AgentTask], merge_tasks_by_id] = Field(default_factory=list)

    # Consumo agregado — reducer aditivo (seguro em fan-out)
    usage: Annotated[dict[str, float], add_usage] = Field(
        default_factory=lambda: {"tokens": 0, "cost_usd": 0.0}
    )

    # Avaliações acumuladas (append-only, auditoria)
    evaluations: Annotated[list[EvaluationResult], operator.add] = Field(
        default_factory=list
    )

    # Escalares — um nó escreve por vez
    phase: Annotated[WorkflowPhase, keep_latest] = WorkflowPhase.LOADING_CONTEXT
    iteration: Annotated[int, keep_latest] = 0
    context: Annotated[dict[str, Any], keep_latest] = Field(default_factory=dict)
    final_output: Annotated[str | None, keep_latest] = None
    error: Annotated[str | None, keep_latest] = None
    human_decision: Annotated[str | None, keep_latest] = (
        None  # preenchido pós-interrupt
    )
    delivery_result: Annotated[DeliveryResult | None, keep_latest] = None
    workspace: Annotated[WorkspaceLease | None, keep_latest] = None
    build_strategy: Annotated[BuildProfileSelection | None, keep_latest] = None
    factory_stage: Annotated[FactoryStage | None, keep_latest] = None

    # ----------------------------------------------------------------------
    # Views derivadas — substituem completed_tasks / failed_tasks
    # ----------------------------------------------------------------------

    @property
    def completed_tasks(self) -> list[AgentTask]:
        return [t for t in self.plan if t.status == TaskStatus.COMPLETED]

    @property
    def escalated_tasks(self) -> list[AgentTask]:
        return [t for t in self.plan if t.status == TaskStatus.ESCALATED]

    @property
    def incomplete_tasks(self) -> list[AgentTask]:
        return [t for t in self.plan if t.status != TaskStatus.COMPLETED]

    @property
    def tasks_needing_replan(self) -> list[AgentTask]:
        return [
            t for t in self.plan if t.status in {TaskStatus.REJECTED, TaskStatus.FAILED}
        ]

    @property
    def redispatchable_tasks(self) -> list[AgentTask]:
        """Contrato do replan: SÓ estas voltam ao dispatch."""
        return [t for t in self.plan if t.status.is_redispatchable]

    @property
    def ready_tasks(self) -> list[AgentTask]:
        """Redispatcháveis com todas as dependências COMPLETED — o fan-out
        do dispatch_tasks itera exatamente sobre esta lista."""
        done = {t.id for t in self.completed_tasks}
        return [
            t
            for t in self.redispatchable_tasks
            if all(dep in done for dep in t.dependencies)
        ]

    @property
    def all_approved(self) -> bool:
        return bool(self.plan) and all(
            t.status == TaskStatus.COMPLETED for t in self.plan
        )

    # ----------------------------------------------------------------------
    # Circuit breaker
    # ----------------------------------------------------------------------

    @property
    def budget_exhausted(self) -> bool:
        elapsed = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        return (
            self.usage.get("tokens", 0) >= self.budget.max_tokens
            or self.usage.get("cost_usd", 0.0) >= self.budget.max_cost_usd
            or elapsed >= self.budget.max_wall_clock_seconds
        )

    @property
    def iterations_exhausted(self) -> bool:
        return self.iteration >= self.budget.max_iterations


# --------------------------------------------------------------------------
# Router do judge — corrige o problema "rejeitado vira síntese"
# --------------------------------------------------------------------------


def judge_router(state: WorkflowState) -> str:
    """Destinos possíveis: synthesize | replan | human_gate.

    Exaurir iterações ou budget NUNCA sintetiza resultado reprovado:
    vai para o gate humano (interrupt), que decide entre aceitar parcial,
    autorizar mais iterações ou abortar.
    """
    if state.all_approved:
        return "synthesize"
    if state.escalated_tasks or state.budget_exhausted:
        return "human_gate"
    if state.tasks_needing_replan:
        if state.iterations_exhausted:
            return "human_gate"
        return "replan"
    if state.ready_tasks:
        return "continue_execution"
    # Plano incompleto sem tarefa pronta: dependência inválida/bloqueada.
    if state.incomplete_tasks:
        return "human_gate"
    return "replan"
