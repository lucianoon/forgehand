"""Contratos entre o grafo e o mundo dos LLMs/ferramentas.

Nenhum nó importa provider de LLM. Planner, executores, judge, advisor,
publisher e runtime da factory chegam por protocolos (regra 1) e são
reunidos em NodeDependencies, que os módulos de fase (app.graph.phase_*)
recebem para construir os nós — testáveis com fakes, sem tocar em
app/providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

from pydantic import BaseModel, Field

from app.factory.workspace import WorkspaceManager, WorkspaceRuntimeFactory
from app.graph.state import DeliveryConfig, DeliveryResult, WorkflowState
from app.models.build import BuildProfile
from app.models.build_execution import BuildRunResult
from app.models.factory import (
    BuildProfileSelection,
    FactoryStage,
    WorkOrder,
    WorkspaceLease,
)
from app.models.task import AdvisorTrigger, AgentTask, EvaluationResult


# --------------------------------------------------------------------------
# Protocolos — a fronteira entre o grafo e o mundo dos LLMs/ferramentas
# --------------------------------------------------------------------------


class Planner(Protocol):
    async def create_plan(
        self, request: str, context: dict[str, Any]
    ) -> PlanningOutcome | list[AgentTask]: ...


class Executor(Protocol):
    async def execute(self, task: AgentTask, context: dict[str, Any]) -> dict[str, Any]:
        """Retorna {"result": dict, "agent": str, "model": str, "tokens": int, "cost_usd": float}."""
        ...


class Judge(Protocol):
    async def evaluate(
        self, task: AgentTask, context: dict[str, Any]
    ) -> JudgingOutcome | EvaluationResult: ...


class ExecutorRegistry(Protocol):
    def select(self, task: AgentTask) -> Executor: ...


class MemoryStore(Protocol):
    async def load_context(
        self, project_id: str, request: str = ""
    ) -> dict[str, Any]: ...
    async def persist(self, state: WorkflowState) -> None: ...


class BuildStrategySelector(Protocol):
    def select(
        self, order: WorkOrder, lease: WorkspaceLease
    ) -> BuildProfileSelection: ...

    def profile_for(self, selection: BuildProfileSelection) -> BuildProfile: ...


class StrategyAuditRecorder(Protocol):
    async def __call__(
        self,
        *,
        workflow_id: str,
        project_id: str,
        client_id: str,
        repository: str,
        selection: BuildProfileSelection,
    ) -> None: ...


class BuildRunner(Protocol):
    async def run(
        self, lease: WorkspaceLease, selection: BuildProfileSelection
    ) -> BuildRunResult: ...


class BuildAuditRecorder(Protocol):
    async def __call__(
        self,
        *,
        project_id: str,
        client_id: str,
        lease: WorkspaceLease,
        selection: BuildProfileSelection,
        report: BuildRunResult,
    ) -> None: ...


class ExecutionPayload(BaseModel):
    """Input schema do worker — o que cada Send() carrega."""

    task: AgentTask
    project_id: str
    context: dict[str, Any]
    workspace: WorkspaceLease | None = None
    factory_stage: FactoryStage | None = None
    build_strategy: BuildProfileSelection | None = None
    owner_client_id: str = ""
    token_allowance: int | None = Field(default=None, ge=0)
    cost_allowance_usd: float | None = Field(default=None, ge=0)


class UsageReport(BaseModel):
    tokens: int = 0
    cost_usd: float = 0.0


class PlanningOutcome(BaseModel):
    plan: list[AgentTask]
    usage: UsageReport = Field(default_factory=UsageReport)


class JudgingOutcome(BaseModel):
    evaluation: EvaluationResult
    usage: UsageReport = Field(default_factory=UsageReport)


class AdvisingOutcome(BaseModel):
    diagnosis: str
    guidance: list[str] = Field(default_factory=list)
    escalate_tier: bool = False
    usage: UsageReport = Field(default_factory=UsageReport)


class Advisor(Protocol):
    async def advise(
        self,
        trigger: AdvisorTrigger,
        evaluations: list[EvaluationResult],
        context: dict[str, Any],
    ) -> AdvisingOutcome: ...


class DeliveryPublisher(Protocol):
    """Publica os artefatos aprovados (PR) e, se configurado, espera o CI.
    Nunca levanta: erro vira DeliveryResult(ci_state="error")."""

    async def publish(
        self,
        *,
        config: DeliveryConfig,
        workflow_id: str,
        project_id: str,
        files: list[dict[str, str]],
        deletions: list[str],
        summary: str,
        details: str = "",
    ) -> DeliveryResult: ...


# --------------------------------------------------------------------------
# Dependências injetadas — compartilhadas por todas as fases
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeDependencies:
    """Tudo que build_nodes recebe, num só objeto imutável.

    Em factory mode, planner/registry/judge são substituídos por versões
    presas ao workspace (runtime_factory); os métodos active_* escolhem.
    """

    planner: Planner
    registry: ExecutorRegistry
    judge: Judge
    memory: MemoryStore
    advisor: Advisor | None = None
    delivery: DeliveryPublisher | None = None
    workspace_manager: WorkspaceManager | None = None
    runtime_factory: WorkspaceRuntimeFactory | None = None
    build_strategy_selector: BuildStrategySelector | None = None
    strategy_audit_recorder: StrategyAuditRecorder | None = None
    build_runner: BuildRunner | None = None
    build_audit_recorder: BuildAuditRecorder | None = None

    def active_planner(self, state: WorkflowState) -> Planner:
        if state.workspace is not None and self.runtime_factory is not None:
            return cast(Planner, self.runtime_factory.build_planner(state.workspace))
        return self.planner

    def active_registry(self, lease: WorkspaceLease | None) -> ExecutorRegistry:
        if lease is not None and self.runtime_factory is not None:
            return cast(ExecutorRegistry, self.runtime_factory.build_registry(lease))
        return self.registry

    def active_judge(self, lease: WorkspaceLease | None) -> Judge:
        if lease is not None and self.runtime_factory is not None:
            return cast(Judge, self.runtime_factory.build_judge(lease))
        return self.judge
