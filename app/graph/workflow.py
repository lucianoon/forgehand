"""Montagem do workflow.

Topologia:

  load_context → create_plan → [route_to_execution]
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
    ("app.graph.nodes", "ExecutionPayload"),  # viaja nos Send pendentes
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
) -> Any:
    nodes = build_nodes(planner, registry, judge, memory, advisor)

    graph = StateGraph(WorkflowState)

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
    graph.add_node("persist_memory", nodes["persist_memory"])

    graph.set_entry_point("load_context")
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
            "synthesize": "synthesize",
            "abort": "abort",
        },
    )

    graph.add_edge("authorize_retry", "replan")

    graph.add_edge("synthesize", "persist_memory")
    graph.add_edge("abort", "persist_memory")
    graph.add_edge("persist_memory", END)

    return graph.compile(checkpointer=checkpointer)
