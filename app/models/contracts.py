"""Contratos que atravessam camadas.

Estes tipos são a moeda entre `app/graph/` (orquestração), `app/agents/`
(planner, executores, judge, advisor) e `app/infrastructure/` (settings,
workspace). Morar em `app/models/` mantém a dependência apontando para baixo:
antes, `app/agents/judge.py` importava de `app/graph/nodes.py` e
`app/infrastructure/settings.py` importava de `app/agents/executor.py` — as
duas na direção contrária à que os docstrings das camadas afirmam.

`app/models/` não importa de nenhuma outra camada, então é o único lugar onde
um contrato compartilhado não cria ciclo.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.task import AgentTask, EvaluationResult


class UsageReport(BaseModel):
    """Consumo de uma chamada de agente, agregado pelo budget do workflow."""

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


class ExecutionStrategy(BaseModel):
    """Como uma capability executa: aplica arquivos, valida, autocorrige."""

    apply_files: bool = True
    run_objective_validation: bool = True
    allow_autocorrect: bool = True
