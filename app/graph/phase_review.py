"""Fase de revisão: replan com advisor, gate humano, retry, síntese e abort.

GATE HUMANO: human_gate usa interrupt(). O grafo pausa no checkpointer e só
continua com Command(resume={...}) — decisão humana rastreável. Só o advisor
escala tier (regra 8); COMPLETED nunca re-executa.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from langgraph.types import interrupt

from app.factory.delivery import factory_ready_for_review
from app.graph.build_evidence import build_validation_section, latest_build_report
from app.graph.contracts import AdvisingOutcome, NodeDependencies
from app.graph.state import WorkflowPhase, WorkflowState
from app.models.task import AdvisorTrigger, AgentTask, TaskAttempt, TaskStatus


def repeated_diff_detected(task: AgentTask) -> bool:
    """Mesma solução em tentativas seguidas: diff_paths não vazios e
    idênticos nos dois últimos operational_summary."""
    if len(task.attempts) < 2:
        return False

    def diff_paths(attempt: TaskAttempt) -> list[Any]:
        summary = attempt.operational_summary or {}
        paths = summary.get("diff_paths")
        return paths if isinstance(paths, list) else []

    last = diff_paths(task.attempts[-1])
    return bool(last) and last == diff_paths(task.attempts[-2])


def build_review_nodes(deps: NodeDependencies) -> dict[str, Any]:
    async def consult_advisor(
        task: AgentTask, state: WorkflowState
    ) -> AdvisingOutcome | None:
        """Consulta condicionada aos sinais OBJETIVOS do AdvisorTrigger.
        Sem tentativa registrada não há falha a diagnosticar; falha do
        próprio advisor nunca bloqueia o replan — ele é conselheiro."""
        if deps.advisor is None or task.attempt_count == 0:
            return None
        trigger = AdvisorTrigger(
            task=task,
            judge_rejections=sum(
                1
                for ev in state.evaluations
                if ev.task_id == task.id and not ev.approved
            ),
            repeated_diff_detected=repeated_diff_detected(task),
        )
        if not trigger.should_consult:
            return None
        try:
            return await deps.advisor.advise(trigger, state.evaluations, state.context)
        except Exception:  # noqa: BLE001
            return None

    async def replan(state: WorkflowState) -> dict[str, Any]:
        """Anexa required_changes do judge — e, quando os sinais do
        AdvisorTrigger disparam, diagnóstico e orientação do advisor — à
        descrição das tarefas rejeitadas. Só o advisor escala tier (regra 8).
        Contrato: só toca tarefas redispatcháveis — COMPLETED nunca re-executa."""
        changes_by_task: dict[UUID, list[str]] = {}
        for ev in state.evaluations:
            if ev.required_changes:
                changes_by_task.setdefault(ev.task_id, []).extend(ev.required_changes)

        updates = []
        total_tokens = 0
        total_cost = 0.0
        for task in state.redispatchable_tasks:
            changes = changes_by_task.get(task.id)
            advice = await consult_advisor(task, state)
            if advice is not None:
                total_tokens += advice.usage.tokens
                total_cost += advice.usage.cost_usd
            if not changes and advice is None:
                continue
            description = task.description
            if changes:
                description += "\n\nCorreções exigidas pelo judge:\n" + "\n".join(
                    f"- {c}" for c in changes
                )
            task_update: dict[str, Any] = {
                "status": TaskStatus.PENDING,
                "updated_at": datetime.now(timezone.utc),
            }
            if advice is not None:
                description += (
                    f"\n\nDiagnóstico do advisor:\n{advice.diagnosis}\n"
                    "\nOrientação do advisor para esta tentativa:\n"
                    + "\n".join(f"- {g}" for g in advice.guidance)
                )
                if advice.escalate_tier:
                    task_update["tier_escalated"] = True
            task_update["description"] = description
            updates.append(task.model_copy(update=task_update))
        return {
            "plan": updates,
            "iteration": state.iteration + 1,
            "usage": {"tokens": total_tokens, "cost_usd": total_cost},
            "phase": WorkflowPhase.EXECUTING,
        }

    async def continue_execution(state: WorkflowState) -> dict[str, Any]:
        """Avança para a próxima camada do DAG sem consumir uma iteração."""
        return {"phase": WorkflowPhase.EXECUTING}

    # ------------------------------------------------------------------
    # Gate humano — resolve o "rejeitado vira síntese"
    # ------------------------------------------------------------------
    async def human_gate(state: WorkflowState) -> dict[str, Any]:
        ci_failed = (
            state.delivery_result is not None and state.delivery_result.ci_failed
        )
        unsupported_strategy = (
            state.build_strategy is not None
            and state.build_strategy.selection_reason == "unsupported"
        )
        if unsupported_strategy:
            reason = "unsupported_build_strategy"
        elif (
            state.work_order is not None
            and state.all_approved
            and state.delivery_result is not None
            and not factory_ready_for_review(state)
        ):
            reason = f"factory_delivery_{state.delivery_result.ci_state}"
        elif state.budget_exhausted:
            reason = "budget_exhausted"
        elif state.iterations_exhausted and state.tasks_needing_replan:
            reason = (
                "ci_failed_iterations_exhausted"
                if ci_failed
                else "iterations_exhausted"
            )
        elif state.escalated_tasks:
            reason = "tasks_escalated"
        elif ci_failed:
            reason = "ci_failed"
        else:
            reason = "dependency_deadlock"
        options = (
            ["retry", "abort"]
            if unsupported_strategy or state.work_order is not None
            else [
                "accept_partial",
                "retry",
                "abort",
            ]
        )
        decision = interrupt(
            {
                "reason": reason,
                "build_strategy": (
                    state.build_strategy.model_dump(mode="json")
                    if state.build_strategy is not None
                    else None
                ),
                "delivery": (
                    state.delivery_result.model_dump(mode="json")
                    if state.delivery_result is not None
                    else None
                ),
                "escalated_tasks": [
                    {
                        "id": str(t.id),
                        "title": t.title,
                        "last_failure": t.attempts[-1].failure_reason
                        if t.attempts
                        else None,
                    }
                    for t in state.escalated_tasks
                ],
                "usage": state.usage,
                "iteration": state.iteration,
                "options": options,
            }
        )
        return {"human_decision": decision, "phase": WorkflowPhase.AWAITING_HUMAN}

    def human_router(state: WorkflowState) -> str:
        if (
            state.build_strategy is not None
            and state.build_strategy.selection_reason == "unsupported"
        ):
            return (
                "select_build_strategy" if state.human_decision == "retry" else "abort"
            )
        if state.work_order is not None:
            if state.human_decision != "retry":
                return "abort"
            if state.all_approved and state.delivery_result is not None:
                return "publish_delivery"
        match state.human_decision:
            case "retry":
                return "authorize_retry"
            case "accept_partial":
                return "synthesize"
            case _:
                return "abort"

    async def authorize_retry(state: WorkflowState) -> dict[str, Any]:
        """Concede headroom explícito para uma tentativa humana adicional."""
        budget = state.budget
        elapsed = (datetime.now(timezone.utc) - state.started_at).total_seconds()
        updates: dict[str, int | float] = {
            "max_iterations": budget.max_iterations + 1,
        }
        if state.usage.get("tokens", 0) >= budget.max_tokens:
            updates["max_tokens"] = int(state.usage["tokens"]) + max(
                budget.max_tokens, int(state.usage["tokens"])
            )
        if state.usage.get("cost_usd", 0.0) >= budget.max_cost_usd:
            updates["max_cost_usd"] = float(state.usage["cost_usd"]) + max(
                budget.max_cost_usd, float(state.usage["cost_usd"])
            )
        if elapsed >= budget.max_wall_clock_seconds:
            updates["max_wall_clock_seconds"] = int(elapsed) + max(
                60, budget.max_wall_clock_seconds // 4
            )
        retryable_escalations: list[AgentTask] = []
        for task in state.escalated_tasks:
            task_budget = task.budget
            budget_updates: dict[str, int | float] = {}
            if task_budget.consumed_tokens >= task_budget.max_tokens:
                budget_updates["max_tokens"] = task_budget.consumed_tokens + max(
                    task_budget.max_tokens, task_budget.consumed_tokens
                )
            if task_budget.consumed_cost_usd >= task_budget.max_cost_usd:
                budget_updates["max_cost_usd"] = task_budget.consumed_cost_usd + max(
                    task_budget.max_cost_usd,
                    task_budget.consumed_cost_usd,
                )
            retryable_escalations.append(
                task.model_copy(
                    update={
                        "status": TaskStatus.REJECTED,
                        "budget": task_budget.model_copy(update=budget_updates),
                        "updated_at": datetime.now(timezone.utc),
                    }
                )
            )
        return {
            "budget": budget.model_copy(update=updates),
            "plan": retryable_escalations,
            "human_decision": None,
            "phase": WorkflowPhase.REPLANNING,
        }

    async def synthesize(state: WorkflowState) -> dict[str, Any]:
        parts = [f"# Entrega — {state.project_id}\n"]
        if state.incomplete_tasks:
            parts.append(
                "⚠️ Entrega PARCIAL aceita por decisão humana. "
                "Tarefas não concluídas: "
                f"{[(t.title, t.status.value) for t in state.incomplete_tasks]}\n"
            )
        for t in state.completed_tasks:
            parts.append(f"## {t.title}\n{t.result}")
        report = latest_build_report(state.plan)
        if report is not None:
            parts.append(build_validation_section(report))
        return {
            "final_output": "\n".join(parts),
            "phase": WorkflowPhase.SYNTHESIZING,
        }

    async def abort(state: WorkflowState) -> dict[str, Any]:
        return {
            "final_output": f"Workflow abortado por decisão humana. Uso: {state.usage}",
            "phase": WorkflowPhase.FAILED,
        }

    return {
        "replan": replan,
        "continue_execution": continue_execution,
        "human_gate": human_gate,
        "human_router": human_router,
        "authorize_retry": authorize_retry,
        "synthesize": synthesize,
        "abort": abort,
    }
