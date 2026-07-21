"""Nós do workflow Agent Forge.

Decisões estruturais:

1. INJEÇÃO DE DEPENDÊNCIA: nenhum nó importa provider de LLM. Planner,
   executores e judge chegam por protocolos — os nós são testáveis com fakes
   e o acoplamento a fornecedor fica confinado em app/providers (regra 1).

2. FAN-OUT VIA Send(): o roteador `route_to_execution` emite um Send por
   tarefa em state.ready_tasks. Cada worker retorna APENAS as tarefas que
   tocou; o reducer merge_tasks_by_id consolida sem colisão.

3. TIMEOUT NO WORKER: asyncio.wait_for com task.timeout_seconds (regra 4).
   Estouro vira FAILED/ESCALATED — nunca exceção não tratada no grafo.

4. GATE HUMANO: human_gate usa interrupt(). O grafo pausa no checkpointer
   e só continua com Command(resume={...}) — decisão humana rastreável.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from langgraph.types import Send, interrupt
from pydantic import BaseModel, Field

from app.agents.validation import format_validation_feedback
from app.graph.state import WorkflowPhase, WorkflowState
from app.models.task import (
    AdvisorTrigger,
    AgentTask,
    EvaluationResult,
    TaskAttempt,
    TaskStatus,
)


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


class ExecutionPayload(BaseModel):
    """Input schema do worker — o que cada Send() carrega."""

    task: AgentTask
    project_id: str
    context: dict[str, Any]


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


# --------------------------------------------------------------------------
# Fábrica de nós
# --------------------------------------------------------------------------


def build_nodes(
    planner: Planner,
    registry: ExecutorRegistry,
    judge: Judge,
    memory: MemoryStore,
    advisor: Advisor | None = None,
) -> dict[str, Any]:
    def attempt_operational_summary(
        result: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(result, dict):
            return None
        workspace = result.get("workspace")
        if not isinstance(workspace, dict):
            return None
        command_feedback = workspace.get("command_feedback")
        file_diffs = workspace.get("file_diffs")
        operation_history = workspace.get("operation_history")
        git_snapshot = workspace.get("git_snapshot")
        strategy = workspace.get("strategy")
        autocorrect = workspace.get("autocorrect")
        validation_feedback = (
            command_feedback if isinstance(command_feedback, list) else []
        )
        return {
            "applied_files": workspace.get("applied_files", []),
            "diff_paths": [
                item.get("path")
                for item in file_diffs
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            ]
            if isinstance(file_diffs, list)
            else [],
            "command_feedback": validation_feedback,
            "validation_feedback_text": format_validation_feedback(validation_feedback),
            "executed_commands": [
                item.get("command")
                for item in validation_feedback
                if isinstance(item, dict) and isinstance(item.get("command"), str)
            ]
            if validation_feedback
            else [],
            "operation_steps": len(operation_history)
            if isinstance(operation_history, list)
            else 0,
            "git_status": git_snapshot.get("status")
            if isinstance(git_snapshot, dict)
            else None,
            "strategy": strategy if isinstance(strategy, dict) else None,
            "autocorrect": autocorrect if isinstance(autocorrect, dict) else None,
        }

    async def load_context(state: WorkflowState) -> dict[str, Any]:
        ctx = await memory.load_context(state.project_id, state.request)
        return {"context": ctx, "phase": WorkflowPhase.PLANNING}

    async def create_plan(state: WorkflowState) -> dict[str, Any]:
        if state.plan:
            # Replays do nó de planning não podem chamar o planner novamente
            # nem anexar um segundo plano ao estado já persistido.
            return {
                "plan": state.plan,
                "usage": {"tokens": 0, "cost_usd": 0.0},
                "phase": WorkflowPhase.EXECUTING,
            }
        raw = await planner.create_plan(state.request, state.context)
        if isinstance(raw, PlanningOutcome):
            plan = raw.plan
            usage = raw.usage.model_dump()
        else:  # compatibilidade com implementações do protocolo existentes
            plan = raw
            usage = {"tokens": 0, "cost_usd": 0.0}
        if not plan:
            raise ValueError("Planner retornou plano vazio.")
        return {"plan": plan, "usage": usage, "phase": WorkflowPhase.EXECUTING}

    # ------------------------------------------------------------------
    # Fan-out: função de aresta condicional, não nó. Emite um Send por
    # tarefa pronta; o LangGraph executa os workers no mesmo superstep
    # e sincroniza todos antes de evaluate_results.
    # ------------------------------------------------------------------
    def route_to_execution(state: WorkflowState) -> list[Send] | str:
        if state.budget_exhausted:
            return "human_gate"
        ready = state.ready_tasks
        if not ready:
            # nada executável: ou tudo pronto (avalia) ou deadlock de deps
            return "evaluate_results"
        results_by_id = {t.id: t.result for t in state.plan if t.result is not None}
        sends: list[Send] = []
        dispatched_by_agent: dict[str, int] = {}
        for t in ready:
            dispatch_policy = getattr(registry, "dispatch_policy", None)
            if dispatch_policy is not None:
                agent_name, limit = dispatch_policy(t)
                current = dispatched_by_agent.get(agent_name, 0)
                if current >= limit:
                    continue
                dispatched_by_agent[agent_name] = current + 1
            # só dependências DIRETAS — mantém o contexto (e os tokens) limitados
            dep_results = {
                str(d): results_by_id[d] for d in t.dependencies if d in results_by_id
            }
            ctx = dict(state.context)
            if dep_results:
                ctx["dependency_results"] = dep_results
            if t.evidence_ids:
                ctx["task_evidence_ids"] = t.evidence_ids
            sends.append(
                Send(
                    "execute_task",
                    ExecutionPayload(task=t, project_id=state.project_id, context=ctx),
                )
            )
        if not sends:
            # Nada foi despachado apesar de existirem tarefas prontas: evitar
            # loop vazio e pedir intervenção humana para revisar a política.
            return "human_gate"
        return sends

    async def execute_task(payload: ExecutionPayload) -> dict[str, Any]:
        task = payload.task
        executor = registry.select(task)
        started = datetime.now(timezone.utc)
        attempt_number = task.attempt_count + 1

        if task.budget.exhausted:
            attempt = TaskAttempt(
                attempt_number=attempt_number,
                agent_name=task.assigned_agent or "unknown",
                model="unknown",
                started_at=started,
                finished_at=datetime.now(timezone.utc),
                outcome=TaskStatus.FAILED,
                failure_reason="budget da tarefa esgotado antes da execução",
            )
            failed = task.model_copy(
                update={
                    "attempts": [*task.attempts, attempt],
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            return {
                "plan": [
                    failed.model_copy(
                        update={"status": failed.next_status_after_failure()}
                    )
                ]
            }

        try:
            outcome = await asyncio.wait_for(
                executor.execute(task, payload.context),
                timeout=task.timeout_seconds,
            )
            tokens = int(outcome.get("tokens", 0))
            cost = float(outcome.get("cost_usd", 0.0))
            charged_budget = task.budget.charge(tokens, cost)
            budget_exceeded = charged_budget.exceeded
            attempt = TaskAttempt(
                attempt_number=attempt_number,
                agent_name=outcome.get("agent", "unknown"),
                model=outcome.get("model", "unknown"),
                started_at=started,
                finished_at=datetime.now(timezone.utc),
                outcome=TaskStatus.FAILED if budget_exceeded else TaskStatus.RUNNING,
                failure_reason=(
                    "execução ultrapassou o budget da tarefa"
                    if budget_exceeded
                    else None
                ),
                tokens_used=tokens,
                cost_usd=cost,
                operational_summary=attempt_operational_summary(outcome.get("result")),
            )
            updated = task.model_copy(
                update={
                    "status": TaskStatus.RUNNING,
                    "result": outcome.get("result"),
                    "attempts": [*task.attempts, attempt],
                    "budget": charged_budget,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            if budget_exceeded:
                updated = updated.model_copy(
                    update={"status": updated.next_status_after_failure()}
                )
            return {"plan": [updated], "usage": {"tokens": tokens, "cost_usd": cost}}

        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            reason = (
                f"timeout após {task.timeout_seconds}s"
                if isinstance(exc, asyncio.TimeoutError)
                else f"{type(exc).__name__}: {exc}"
            )
            attempt = TaskAttempt(
                attempt_number=attempt_number,
                agent_name=task.assigned_agent or "unknown",
                model="unknown",
                started_at=started,
                finished_at=datetime.now(timezone.utc),
                outcome=TaskStatus.FAILED,
                failure_reason=reason,
            )
            failed = task.model_copy(
                update={
                    "attempts": [*task.attempts, attempt],
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            failed = failed.model_copy(
                update={"status": failed.next_status_after_failure()}
            )
            return {"plan": [failed]}

    async def evaluate_results(state: WorkflowState) -> dict[str, Any]:
        """Julga tarefas em RUNNING. Sinais objetivos têm veto (validator
        do EvaluationResult garante isso no contrato)."""
        updates: list[AgentTask] = []
        evaluations: list[EvaluationResult] = []
        total_tokens = 0
        total_cost = 0.0

        for task in state.plan:
            if task.status != TaskStatus.RUNNING:
                continue
            try:
                raw = await judge.evaluate(task, state.context)
            except Exception as exc:  # noqa: BLE001
                reason = f"judge {type(exc).__name__}: {exc}"
                attempts = list(task.attempts)
                if attempts:
                    attempts[-1] = attempts[-1].model_copy(
                        update={
                            "outcome": TaskStatus.FAILED,
                            "failure_reason": reason,
                            "finished_at": datetime.now(timezone.utc),
                        }
                    )
                updates.append(
                    task.model_copy(
                        update={
                            "status": TaskStatus.ESCALATED,
                            "attempts": attempts,
                            "updated_at": datetime.now(timezone.utc),
                        }
                    )
                )
                continue
            if isinstance(raw, JudgingOutcome):
                evaluation = raw.evaluation
                judge_usage = raw.usage
            else:
                evaluation = raw
                judge_usage = UsageReport()
            evaluations.append(evaluation)
            total_tokens += judge_usage.tokens
            total_cost += judge_usage.cost_usd
            if evaluation.approved:
                new_status = TaskStatus.COMPLETED
            else:
                new_status = task.next_status_after_failure()
            updates.append(
                task.model_copy(
                    update={
                        "status": new_status,
                        "updated_at": datetime.now(timezone.utc),
                    }
                )
            )

        return {
            "plan": updates,
            "evaluations": evaluations,
            "usage": {"tokens": total_tokens, "cost_usd": total_cost},
            "phase": WorkflowPhase.EVALUATING,
        }

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

    async def consult_advisor(
        task: AgentTask, state: WorkflowState
    ) -> AdvisingOutcome | None:
        """Consulta condicionada aos sinais OBJETIVOS do AdvisorTrigger.
        Sem tentativa registrada não há falha a diagnosticar; falha do
        próprio advisor nunca bloqueia o replan — ele é conselheiro."""
        if advisor is None or task.attempt_count == 0:
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
            return await advisor.advise(trigger, state.evaluations, state.context)
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
        if state.budget_exhausted:
            reason = "budget_exhausted"
        elif state.iterations_exhausted and state.tasks_needing_replan:
            reason = "iterations_exhausted"
        elif state.escalated_tasks:
            reason = "tasks_escalated"
        else:
            reason = "dependency_deadlock"
        decision = interrupt(
            {
                "reason": reason,
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
                "options": ["accept_partial", "retry", "abort"],
            }
        )
        return {"human_decision": decision, "phase": WorkflowPhase.AWAITING_HUMAN}

    def human_router(state: WorkflowState) -> str:
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
        return {
            "final_output": "\n".join(parts),
            "phase": WorkflowPhase.SYNTHESIZING,
        }

    async def abort(state: WorkflowState) -> dict[str, Any]:
        return {
            "final_output": f"Workflow abortado por decisão humana. Uso: {state.usage}",
            "phase": WorkflowPhase.FAILED,
        }

    async def persist_memory(state: WorkflowState) -> dict[str, Any]:
        await memory.persist(state)
        terminal = (
            WorkflowPhase.FAILED
            if state.phase == WorkflowPhase.FAILED
            else WorkflowPhase.COMPLETED
        )
        return {"phase": terminal}

    return {
        "load_context": load_context,
        "create_plan": create_plan,
        "route_to_execution": route_to_execution,
        "execute_task": execute_task,
        "evaluate_results": evaluate_results,
        "replan": replan,
        "continue_execution": continue_execution,
        "human_gate": human_gate,
        "human_router": human_router,
        "authorize_retry": authorize_retry,
        "synthesize": synthesize,
        "abort": abort,
        "persist_memory": persist_memory,
    }
