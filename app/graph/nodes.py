"""Nós do workflow Forgehand — ponto de montagem.

Decisões estruturais:

1. INJEÇÃO DE DEPENDÊNCIA: nenhum nó importa provider de LLM. Planner,
   executores e judge chegam por protocolos (app.graph.contracts) — os nós
   são testáveis com fakes e o acoplamento a fornecedor fica confinado em
   app/providers (regra 1).

2. UM MÓDULO POR FASE DO GRAFO: cada `build_*_nodes(deps)` recebe as
   mesmas NodeDependencies e devolve só os nós da sua fase:

     app.graph.phase_setup      provision → estratégia → contexto → plano
     app.graph.phase_execution  fan-out via Send(), worker com timeout, join
     app.graph.phase_review     replan/advisor, gate humano, retry, síntese
     app.graph.phase_delivery   PR + CI como sinal objetivo, persistência

   As funções puras sobre evidência de build (veto do judge, seções do
   relatório) vivem em app.graph.build_evidence.

3. FAN-OUT VIA Send() e TIMEOUT NO WORKER: ver app.graph.phase_execution.

4. GATE HUMANO com interrupt(): ver app.graph.phase_review.

Este módulo mantém a API pública histórica: build_nodes(...) e os tipos
re-exportados abaixo, que agentes e testes importam de app.graph.nodes.
"""

from __future__ import annotations

from typing import Any

from app.factory.workspace import WorkspaceManager, WorkspaceRuntimeFactory
from app.graph.call_budget import with_call_budget
from app.graph.contracts import (
    Advisor,
    AdvisingOutcome,
    BuildAuditRecorder,
    BuildRunner,
    BuildStrategySelector,
    DeliveryPublisher,
    ExecutionPayload,
    Executor,
    ExecutorRegistry,
    Judge,
    JudgingOutcome,
    MemoryStore,
    NodeDependencies,
    Planner,
    PlanningOutcome,
    StrategyAuditRecorder,
    UsageReport,
)
from app.graph.phase_delivery import build_delivery_nodes
from app.graph.phase_execution import build_execution_nodes
from app.graph.phase_review import build_review_nodes
from app.graph.phase_setup import build_setup_nodes

__all__ = [
    "Advisor",
    "AdvisingOutcome",
    "BuildAuditRecorder",
    "BuildRunner",
    "BuildStrategySelector",
    "DeliveryPublisher",
    "ExecutionPayload",
    "Executor",
    "ExecutorRegistry",
    "Judge",
    "JudgingOutcome",
    "MemoryStore",
    "NodeDependencies",
    "Planner",
    "PlanningOutcome",
    "StrategyAuditRecorder",
    "UsageReport",
    "build_nodes",
]


def build_nodes(
    planner: Planner,
    registry: ExecutorRegistry,
    judge: Judge,
    memory: MemoryStore,
    advisor: Advisor | None = None,
    delivery: DeliveryPublisher | None = None,
    workspace_manager: WorkspaceManager | None = None,
    runtime_factory: WorkspaceRuntimeFactory | None = None,
    build_strategy_selector: BuildStrategySelector | None = None,
    strategy_audit_recorder: StrategyAuditRecorder | None = None,
    build_runner: BuildRunner | None = None,
    build_audit_recorder: BuildAuditRecorder | None = None,
) -> dict[str, Any]:
    """Monta todos os nós e roteadores do grafo, indexados pelo nome usado
    em app.graph.workflow."""
    deps = NodeDependencies(
        planner=planner,
        registry=registry,
        judge=judge,
        memory=memory,
        advisor=advisor,
        delivery=delivery,
        workspace_manager=workspace_manager,
        runtime_factory=runtime_factory,
        build_strategy_selector=build_strategy_selector,
        strategy_audit_recorder=strategy_audit_recorder,
        build_runner=build_runner,
        build_audit_recorder=build_audit_recorder,
    )
    nodes: dict[str, Any] = {}
    for phase_nodes in (
        build_setup_nodes(deps),
        build_execution_nodes(deps),
        build_review_nodes(deps),
        build_delivery_nodes(deps),
    ):
        overlap = nodes.keys() & phase_nodes.keys()
        if overlap:
            raise ValueError(f"nó definido em mais de uma fase: {sorted(overlap)}")
        nodes.update(phase_nodes)
    for name in ("create_plan", "execute_task", "evaluate_results", "replan"):
        nodes[name] = with_call_budget(nodes[name])
    return nodes
