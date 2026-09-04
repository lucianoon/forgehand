"""Montagem do workflow.

Topologia:

  provision_workspace → select_build_strategy → load_context → create_plan
                                                        │
                                             [route_to_execution]
                                     │
                     ┌───────────────┼─────────────────┐
                     ▼ Send×N        ▼                 ▼
               execute_task    evaluate_results   human_gate
                     │               │                 │
                     └──────►────────┤          [human_router]
                                     │           ┌─────┼─────┐
                              [judge_router]     ▼     ▼     ▼
                              ┌──────┼──────┐ replan synth abort
                              ▼      ▼      ▼            │     │
                           replan  synth  human_gate     └──┬──┘
                              │      │                      ▼
                              └──►[route_to_execution]  persist_memory → END

O checkpointer é obrigatório: sem ele, interrupt() não pausa e o estado
não sobrevive a falhas. Em produção: PostgresSaver; em testes: MemorySaver.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, StateGraph

from app.graph.nodes import ExecutionPayload, build_nodes
from app.graph.state import WorkflowState, judge_router

# Tipos do domínio autorizados no checkpoint (evita bloqueio futuro do
# msgpack e torna a superfície de desserialização explícita e auditável).
DOMAIN_TYPES: list[tuple[str, str]] = [
    ("app.graph.state", "WorkflowPhase"),
    ("app.graph.state", "WorkflowBudget"),
    ("app.models.task", "Capability"),
    ("app.models.task", "TaskStatus"),
    ("app.models.task", "AgentTask"),
    ("app.models.task", "TaskAttempt"),
    ("app.models.task", "TaskBudget"),
    ("app.models.task", "EvaluationResult"),
    ("app.models.build", "BuildPhaseName"),
    ("app.models.build_execution", "BuildOutcome"),
    ("app.models.build_execution", "BuildPhaseResult"),
    ("app.models.build_execution", "BuildRunResult"),
    ("app.models.build_execution", "AcceptanceCaseResult"),
    ("app.models.build_execution", "AcceptanceReport"),
    ("app.graph.state", "DeliveryConfig"),
    ("app.graph.state", "DeliveryResult"),
    ("app.models.factory", "WorkOrderSourceKind"),
    ("app.models.factory", "DirectWorkOrderSource"),
    ("app.models.factory", "GitHubIssueSnapshot"),
    ("app.models.factory", "GitHubIssueWorkOrderSource"),
    ("app.models.factory", "RepositoryTarget"),
    ("app.models.factory", "WorkOrderLimits"),
    ("app.models.factory", "BuildProfileSelection"),
    ("app.models.factory", "DeliveryPolicy"),
    ("app.models.factory", "WorkOrder"),
    ("app.models.factory", "WorkspaceLifecycle"),
    ("app.models.factory", "WorkspaceRetention"),
    ("app.models.factory", "WorkspaceLease"),
    ("app.models.factory", "FactoryStage"),
    # ExecutionPayload viaja nos Send pendentes. Definido em app.graph.contracts;
    # a entrada antiga continua válida para checkpoints gravados antes da
    # separação por fase (o nome segue re-exportado em app.graph.nodes).
    ("app.graph.contracts", "ExecutionPayload"),
    ("app.graph.nodes", "ExecutionPayload"),
]


def build_serde() -> JsonPlusSerializer:
    """Use em qualquer checkpointer: MemorySaver(serde=build_serde()) em
    testes, PostgresSaver(conn, serde=build_serde()) em produção."""
    return JsonPlusSerializer(allowed_msgpack_modules=DOMAIN_TYPES)


def build_workflow(
    planner: Any,
    registry: Any,
    judge: Any,
    memory: Any,
    checkpointer: Any,
    advisor: Any = None,
    delivery: Any = None,
    workspace_manager: Any = None,
    runtime_factory: Any = None,
    build_strategy_selector: Any = None,
    strategy_audit_recorder: Any = None,
    build_runner: Any = None,
    build_audit_recorder: Any = None,
) -> Any:
    nodes = build_nodes(
        planner,
        registry,
        judge,
        memory,
        advisor,
        delivery,
        workspace_manager,
        runtime_factory,
        build_strategy_selector,
        strategy_audit_recorder,
        build_runner,
        build_audit_recorder,
    )

    graph = StateGraph(WorkflowState)

    graph.add_node("provision_workspace", nodes["provision_workspace"])
    graph.add_node("select_build_strategy", nodes["select_build_strategy"])
    graph.add_node("load_context", nodes["load_context"])
    graph.add_node("create_plan", nodes["create_plan"])
    graph.add_node("execute_task", nodes["execute_task"], input_schema=ExecutionPayload)
    graph.add_node("evaluate_results", nodes["evaluate_results"])
    graph.add_node("replan", nodes["replan"])
    graph.add_node("continue_execution", nodes["continue_execution"])
    graph.add_node("human_gate", nodes["human_gate"])
    graph.add_node("synthesize", nodes["synthesize"])
    graph.add_node("abort", nodes["abort"])
    graph.add_node("authorize_retry", nodes["authorize_retry"])
    graph.add_node("publish_delivery", nodes["publish_delivery"])
    graph.add_node("persist_memory", nodes["persist_memory"])

    graph.set_entry_point("provision_workspace")
    graph.add_conditional_edges(
        "provision_workspace",
        nodes["provision_router"],
        {
            "load_context": "select_build_strategy",
            "persist_memory": "persist_memory",
        },
    )
    graph.add_conditional_edges(
        "select_build_strategy",
        nodes["strategy_router"],
        {
            "load_context": "load_context",
            "human_gate": "human_gate",
            "persist_memory": "persist_memory",
        },
    )
    graph.add_edge("load_context", "create_plan")

    # Fan-out: create_plan → N workers (ou direto para avaliação/gate)
    graph.add_conditional_edges(
        "create_plan",
        nodes["route_to_execution"],
        ["execute_task", "evaluate_results", "human_gate"],
    )

    # Join: cada branch já executou E julgou sua tarefa (julgamento
    # incremental); o LangGraph sincroniza todos os Sends antes deste passo
    # e o judge_router decide sobre o estado consolidado.
    graph.add_edge("execute_task", "evaluate_results")

    graph.add_conditional_edges(
        "evaluate_results",
        judge_router,
        {
            "replan": "replan",
            "continue_execution": "continue_execution",
            "synthesize": "synthesize",
            "human_gate": "human_gate",
        },
    )

    # Replan re-despacha apenas redispatcháveis (contrato no nó)
    graph.add_conditional_edges(
        "replan",
        nodes["route_to_execution"],
        ["execute_task", "evaluate_results", "human_gate"],
    )

    graph.add_conditional_edges(
        "continue_execution",
        nodes["route_to_execution"],
        ["execute_task", "evaluate_results", "human_gate"],
    )

    graph.add_conditional_edges(
        "human_gate",
        nodes["human_router"],
        {
            "authorize_retry": "authorize_retry",
            "select_build_strategy": "select_build_strategy",
            "publish_delivery": "publish_delivery",
            "synthesize": "synthesize",
            "abort": "abort",
        },
    )

    graph.add_edge("authorize_retry", "replan")

    # Entrega: synthesize → (publica PR + espera CI) → persiste; CI vermelho
    # reabre as tarefas que publicaram e volta ao replan (bounded por
    # max_iterations); esgotado, gate humano.
    graph.add_conditional_edges(
        "synthesize",
        nodes["delivery_router"],
        {"publish_delivery": "publish_delivery", "persist_memory": "persist_memory"},
    )
    graph.add_conditional_edges(
        "publish_delivery",
        nodes["delivery_result_router"],
        {
            "persist_memory": "persist_memory",
            "replan": "replan",
            "human_gate": "human_gate",
        },
    )
    graph.add_edge("abort", "persist_memory")
    graph.add_edge("persist_memory", END)

    return graph.compile(checkpointer=checkpointer)
