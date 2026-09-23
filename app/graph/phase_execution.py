"""Fase de execução: fan-out, worker por tarefa e consolidação no join.

FAN-OUT VIA Send(): route_to_execution emite um Send por tarefa em
state.ready_tasks. Cada worker executa E JULGA a própria tarefa
(julgamento incremental — a rápida não espera a lenta) e retorna APENAS as
tarefas que tocou; o reducer merge_tasks_by_id consolida sem colisão.

TIMEOUT NO WORKER: asyncio.wait_for com task.timeout_seconds (regra 4)
envolve a chamada ao executor (agente + ferramentas). A validação de build
no sandbox tem timeouts próprios por fase do perfil e o judge roda depois,
fora desse relógio. Estouro vira FAILED/ESCALATED — nunca exceção não
tratada no grafo.

Cada etapa é uma função de módulo que recebe NodeDependencies explicitamente;
build_execution_nodes só as amarra nos nomes usados em app.graph.workflow.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from langgraph.types import Send

from app.factory.acceptance import acceptance_verified
from app.factory.sandbox import BuildRunCancelled
from app.graph.build_evidence import (
    apply_build_veto,
    attach_build_report,
    attempt_operational_summary,
    build_report_from_task,
)
from app.graph.contracts import (
    ExecutionOutcome,
    ExecutionPayload,
    Judge,
    JudgingOutcome,
    NodeDependencies,
    UsageReport,
)
from app.graph.state import WorkflowPhase, WorkflowState
from app.infrastructure.llm_budget import (
    BudgetAdmissionError, CallBudget, active_call_budget, call_budget_scope,
)
from app.infrastructure.tracing import current_trace_id
from app.models.build_execution import BuildOutcome, BuildRunResult
from app.models.factory import FactoryStage
from app.models.task import (
    AgentTask,
    EvaluationResult,
    TaskAttempt,
    TaskBudget,
    TaskStatus,
)


# --------------------------------------------------------------------------
# Fan-out: função de aresta condicional, não nó. Emite um Send por tarefa
# pronta; o LangGraph executa os workers no mesmo superstep e sincroniza
# todos antes de evaluate_results.
# --------------------------------------------------------------------------


def _select_dispatchable(
    deps: NodeDependencies, state: WorkflowState, ready: list[AgentTask]
) -> list[AgentTask]:
    """Aplica a política de despacho por agente do registry ativo."""
    dispatched_by_agent: dict[str, int] = {}
    # O perfil valida o workspace inteiro. Em factory mode, executar uma
    # tarefa por vez evita que duas mutações concorrentes contaminem a
    # evidência ou disputem o mesmo sandbox do workflow.
    dispatchable = ready[:1] if state.work_order is not None else ready
    selected_tasks: list[AgentTask] = []
    for t in dispatchable:
        selected_registry = deps.active_registry(state.workspace)
        dispatch_policy = getattr(selected_registry, "dispatch_policy", None)
        if dispatch_policy is not None:
            agent_name, limit = dispatch_policy(t)
            current = dispatched_by_agent.get(agent_name, 0)
            if current >= limit:
                continue
            dispatched_by_agent[agent_name] = current + 1
        selected_tasks.append(t)
    return selected_tasks


def _per_task_allowances(state: WorkflowState, count: int) -> tuple[int, float]:
    """Divide o que resta do budget do workflow entre as tarefas despachadas."""
    token_allowance = max(
        0, state.budget.max_tokens - int(state.usage.get("tokens", 0))
        - int(state.usage.get("unconfirmed_tokens", 0)),
    ) // count
    cost_allowance = max(
        0.0, state.budget.max_cost_usd - state.usage.get("cost_usd", 0.0)
        - state.usage.get("unconfirmed_cost_usd", 0.0),
    ) / count
    return token_allowance, cost_allowance


def route_ready_tasks(
    deps: NodeDependencies, state: WorkflowState
) -> list[Send] | str:
    if state.budget_exhausted:
        return "human_gate"
    ready = state.ready_tasks
    if not ready:
        # nada executável: ou tudo pronto (avalia) ou deadlock de deps
        return "evaluate_results"
    results_by_id = {t.id: t.result for t in state.plan if t.result is not None}
    selected_tasks = _select_dispatchable(deps, state, ready)
    if not selected_tasks:
        return "human_gate"
    token_allowance, cost_allowance = _per_task_allowances(state, len(selected_tasks))
    sends: list[Send] = []
    for t in selected_tasks:
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
                ExecutionPayload(
                    task=t,
                    project_id=state.project_id,
                    context=ctx,
                    workspace=state.workspace,
                    factory_stage=state.factory_stage,
                    build_strategy=state.build_strategy,
                    owner_client_id=state.owner_client_id,
                    token_allowance=token_allowance,
                    cost_allowance_usd=cost_allowance,
                ),
            )
        )
    if not sends:
        # Nada foi despachado apesar de existirem tarefas prontas: evitar
        # loop vazio e pedir intervenção humana para revisar a política.
        return "human_gate"
    return sends


# --------------------------------------------------------------------------
# Julgamento e validação de build
# --------------------------------------------------------------------------


async def judge_task(
    task: AgentTask, context: dict[str, Any], selected_judge: Judge
) -> tuple[AgentTask, EvaluationResult | None, UsageReport]:
    """Julga uma tarefa executada e devolve a cópia com status final.

    Falha do judge escala a tarefa (nunca fica presa em RUNNING);
    sinais objetivos têm veto (validator do EvaluationResult)."""
    try:
        raw = await selected_judge.evaluate(task, context)
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
        escalated = task.model_copy(
            update={
                "status": TaskStatus.ESCALATED,
                "attempts": attempts,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        return escalated, None, UsageReport()
    if isinstance(raw, JudgingOutcome):
        evaluation = raw.evaluation
        judge_usage = raw.usage
    else:
        evaluation = raw
        judge_usage = UsageReport()
    evaluation = apply_build_veto(evaluation, build_report_from_task(task))
    new_status = (
        TaskStatus.COMPLETED
        if evaluation.approved
        else task.next_status_after_failure()
    )
    # A tentativa julgada recebe o veredito: sem isto ficava RUNNING para
    # sempre no histórico, mesmo com a tarefa COMPLETED ou REJECTED.
    attempts = list(task.attempts)
    if attempts and attempts[-1].outcome == TaskStatus.RUNNING:
        attempts[-1] = attempts[-1].model_copy(
            update={
                "outcome": new_status,
                "failure_reason": (
                    None
                    if evaluation.approved
                    else "; ".join(evaluation.failures[:3]) or "reprovado pelo judge"
                ),
            }
        )
    judged = task.model_copy(
        update={
            "status": new_status,
            "attempts": attempts,
            "updated_at": datetime.now(timezone.utc),
        }
    )
    return judged, evaluation, judge_usage


async def record_build_report(
    deps: NodeDependencies, payload: ExecutionPayload, report: BuildRunResult
) -> None:
    if (
        deps.build_audit_recorder is None
        or payload.workspace is None
        or payload.build_strategy is None
    ):
        return
    await deps.build_audit_recorder(
        project_id=payload.project_id,
        client_id=payload.owner_client_id,
        lease=payload.workspace,
        selection=payload.build_strategy,
        report=report,
    )


async def run_build_validation(
    deps: NodeDependencies, payload: ExecutionPayload
) -> BuildRunResult:
    """Roda o perfil de build no sandbox e aplica as rejeições de política."""
    assert payload.workspace is not None and payload.build_strategy is not None
    if deps.build_runner is None:
        build_report = BuildRunResult(
            profile_name=payload.build_strategy.selected_profile,
            profile_digest=payload.build_strategy.profile_digest,
            outcome=BuildOutcome.INFRASTRUCTURE_ERROR,
            error_code="sandbox_runner_unavailable",
        )
    else:
        try:
            build_report = BuildRunResult.model_validate(
                (
                    await deps.build_runner.run(
                        payload.workspace, payload.build_strategy
                    )
                ).model_dump()
            )
        except BuildRunCancelled as exc:
            await record_build_report(deps, payload, exc.report)
            raise
    expected_architecture = payload.build_strategy.architecture_digest
    if (
        build_report.outcome == BuildOutcome.SUCCESS
        and expected_architecture is not None
        and (
            build_report.architecture is None
            or not build_report.architecture.passed
            or build_report.architecture.policy_digest
            != expected_architecture
        )
    ):
        build_report = build_report.model_copy(
            update={
                "outcome": BuildOutcome.POLICY_REJECTION,
                "error_code": "architecture_evidence_missing_or_failed",
            }
        )
    if not acceptance_verified(build_report.acceptance, payload.build_strategy):
        build_report = build_report.model_copy(update={
            "outcome": BuildOutcome.POLICY_REJECTION,
            "error_code": "acceptance_evidence_missing_or_failed",
        })
    await record_build_report(deps, payload, build_report)
    return build_report


# --------------------------------------------------------------------------
# Worker: etapas de execute_task
# --------------------------------------------------------------------------


def failed_attempt(
    payload: ExecutionPayload,
    *,
    attempt_number: int,
    started: datetime,
    reason: str,
) -> dict[str, Any]:
    """Tentativa que não chegou a produzir resultado: budget ou exceção."""
    task = payload.task
    call_budget = active_call_budget()
    tokens = call_budget.tokens if call_budget is not None else 0
    cost = call_budget.cost_usd if call_budget is not None else 0.0
    attempt = TaskAttempt(
        attempt_number=attempt_number,
        agent_name=task.assigned_agent or "unknown",
        model="unknown",
        started_at=started,
        finished_at=datetime.now(timezone.utc),
        outcome=TaskStatus.FAILED,
        failure_reason=reason,
        tokens_used=tokens,
        cost_usd=cost,
        trace_id=current_trace_id(),
        factory_stage=payload.factory_stage,
        build_strategy=payload.build_strategy,
    )
    failed = task.model_copy(
        update={
            "attempts": [*task.attempts, attempt],
            "budget": task.budget.charge(tokens, cost),
            "updated_at": datetime.now(timezone.utc),
        }
    )
    failed = failed.model_copy(
        update={"status": failed.next_status_after_failure()}
    )
    return {"plan": [failed]}


def _budget_blocked_reason(outcome: ExecutionOutcome) -> str | None:
    """Bloqueio reportado pelo executor ou pelo medidor da chamada ativa."""
    if outcome.budget_blocked_reason:
        return outcome.budget_blocked_reason
    active_budget = active_call_budget()
    blocked = active_budget.blocked_reason if active_budget is not None else None
    return str(blocked) if blocked else None


def _executed_task(
    payload: ExecutionPayload,
    outcome: ExecutionOutcome,
    *,
    charged_budget: TaskBudget,
    budget_blocked: str | None,
    attempt_number: int,
    started: datetime,
) -> AgentTask:
    """Registra a tentativa do executor; RUNNING até validação e judge."""
    task = payload.task
    budget_exceeded = charged_budget.exceeded
    attempt = TaskAttempt(
        attempt_number=attempt_number,
        agent_name=outcome.agent,
        model=outcome.model,
        started_at=started,
        finished_at=datetime.now(timezone.utc),
        outcome=TaskStatus.FAILED if budget_exceeded or budget_blocked else TaskStatus.RUNNING,
        failure_reason=(
            budget_blocked if budget_blocked else
            "execução ultrapassou o budget da tarefa"
            if budget_exceeded
            else None
        ),
        tokens_used=outcome.tokens,
        cost_usd=outcome.cost_usd,
        trace_id=current_trace_id(),
        operational_summary=attempt_operational_summary(outcome.result),
        factory_stage=payload.factory_stage,
        build_strategy=payload.build_strategy,
        build_validation=None,
    )
    return task.model_copy(
        update={
            "status": TaskStatus.RUNNING,
            "result": outcome.result,
            "attempts": [*task.attempts, attempt],
            "budget": charged_budget,
            "updated_at": datetime.now(timezone.utc),
        }
    )


def _budget_stop_update(
    updated: AgentTask, *, budget_blocked: str | None, tokens: int, cost: float
) -> dict[str, Any]:
    stopped = updated.model_copy(
        update={
            "status": TaskStatus.ESCALATED if budget_blocked
            else updated.next_status_after_failure()
        }
    )
    return {
        "plan": [stopped],
        "usage": {"tokens": tokens, "cost_usd": cost},
        **({"budget_blocked_reason": budget_blocked} if budget_blocked else {}),
    }


def _with_build_validation(
    payload: ExecutionPayload, updated: AgentTask, build_report: BuildRunResult
) -> AgentTask:
    """Anexa o relatório de build ao resultado e à tentativa em andamento."""
    result = attach_build_report(updated.result, build_report)
    attempt = updated.attempts[-1].model_copy(update={
        "operational_summary": attempt_operational_summary(result),
        "finished_at": datetime.now(timezone.utc),
        "factory_stage": FactoryStage.VALIDATION,
        "build_validation": build_report,
    })
    return updated.model_copy(update={
        "result": result, "attempts": [*payload.task.attempts, attempt],
    })


async def _judge_executed_task(
    deps: NodeDependencies,
    payload: ExecutionPayload,
    updated: AgentTask,
    build_report: BuildRunResult | None,
    *,
    tokens: int,
    cost: float,
) -> dict[str, Any]:
    # Julgamento incremental: a tarefa é julgada no próprio branch, em
    # paralelo com as demais — a rápida não espera a lenta para receber
    # veredito. O judge_router segue decidindo no join, sobre o estado
    # consolidado.
    judged, evaluation, judge_usage = await judge_task(
        updated,
        {
            **payload.context,
            **(
                {"build_validation": build_report.model_dump(mode="json")}
                if build_report is not None
                else {}
            ),
        },
        deps.active_judge(payload.workspace),
    )
    update: dict[str, Any] = {
        "plan": [judged],
        "usage": {
            "tokens": tokens + judge_usage.tokens,
            "cost_usd": cost + judge_usage.cost_usd,
        },
    }
    if evaluation is not None:
        update["evaluations"] = [evaluation]
    if build_report is not None:
        update["factory_stage"] = FactoryStage.VALIDATION
    return update


def _failure_update(
    payload: ExecutionPayload,
    updated: AgentTask | None,
    exc: BaseException,
    *,
    tokens: int,
    cost: float,
    attempt_number: int,
    started: datetime,
) -> dict[str, Any]:
    """Timeout ou exceção: preserva o resultado já aplicado, se houver."""
    task = payload.task
    reason = (
        f"timeout após {task.timeout_seconds}s"
        if isinstance(exc, asyncio.TimeoutError)
        else f"{type(exc).__name__}: {exc}"
    )
    if updated is None:
        return failed_attempt(
            payload, attempt_number=attempt_number, started=started, reason=reason
        )
    attempt = updated.attempts[-1].model_copy(update={
        "outcome": TaskStatus.FAILED,
        "failure_reason": reason,
        "finished_at": datetime.now(timezone.utc),
    })
    failed = updated.model_copy(update={
        "attempts": [*updated.attempts[:-1], attempt],
        "status": (
            TaskStatus.ESCALATED if isinstance(exc, BudgetAdmissionError)
            else updated.next_status_after_failure()
        ),
    })
    return {
        "plan": [failed],
        "usage": {"tokens": tokens, "cost_usd": cost},
        **({"budget_blocked_reason": reason} if isinstance(exc, BudgetAdmissionError) else {}),
        **({"factory_stage": attempt.factory_stage} if attempt.build_validation is not None else {}),
    }


async def run_task(deps: NodeDependencies, payload: ExecutionPayload) -> dict[str, Any]:
    """Executa, valida e julga uma tarefa (corpo do nó execute_task)."""
    task = payload.task
    executor = deps.active_registry(payload.workspace).select(task)
    started = datetime.now(timezone.utc)
    attempt_number = task.attempt_count + 1
    updated: AgentTask | None = None
    tokens = 0
    cost = 0.0

    if task.budget.exhausted:
        return failed_attempt(
            payload,
            attempt_number=attempt_number,
            started=started,
            reason="budget da tarefa esgotado antes da execução",
        )

    try:
        outcome = ExecutionOutcome.from_executor(
            await asyncio.wait_for(
                executor.execute(task, payload.context),
                timeout=task.timeout_seconds,
            )
        )
        tokens, cost = outcome.tokens, outcome.cost_usd
        charged_budget = task.budget.charge(tokens, cost)
        budget_blocked = _budget_blocked_reason(outcome)
        updated = _executed_task(
            payload,
            outcome,
            charged_budget=charged_budget,
            budget_blocked=budget_blocked,
            attempt_number=attempt_number,
            started=started,
        )
        if charged_budget.exceeded or budget_blocked:
            return _budget_stop_update(
                updated, budget_blocked=budget_blocked, tokens=tokens, cost=cost
            )
        # Keep the applied result before any later validation/judge failure.
        # Only a successful fresh build can attach a validation report.
        build_report: BuildRunResult | None = None
        if payload.workspace is not None and payload.build_strategy is not None:
            build_report = await run_build_validation(deps, payload)
            updated = _with_build_validation(payload, updated, build_report)
        return await _judge_executed_task(
            deps, payload, updated, build_report, tokens=tokens, cost=cost
        )
    except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
        return _failure_update(
            payload,
            updated,
            exc,
            tokens=tokens,
            cost=cost,
            attempt_number=attempt_number,
            started=started,
        )


# --------------------------------------------------------------------------
# Join
# --------------------------------------------------------------------------


async def consolidate_results(
    deps: NodeDependencies, state: WorkflowState
) -> dict[str, Any]:
    """Ponto de consolidação no join. O julgamento acontece de forma
    incremental dentro do branch de execute_task; aqui só se julgam
    tarefas que cheguem ainda em RUNNING — fallback para checkpoints
    criados antes do julgamento incremental."""
    updates: list[AgentTask] = []
    evaluations: list[EvaluationResult] = []
    total_tokens = 0
    total_cost = 0.0

    for task in state.plan:
        if task.status != TaskStatus.RUNNING:
            continue
        remaining = CallBudget(
            max_tokens=max(0, task.budget.max_tokens - task.budget.consumed_tokens
                           - task.budget.unconfirmed_tokens),
            max_cost_usd=max(0.0, task.budget.max_cost_usd - task.budget.consumed_cost_usd
                            - task.budget.unconfirmed_cost_usd),
        )
        # Old checkpoints can reach the fallback without execute_task's
        # scope. Reserve against both this task and the surrounding workflow.
        with call_budget_scope(remaining):
            judged, evaluation, judge_usage = await judge_task(
                task, state.context, deps.active_judge(state.workspace)
            )
        judged = judged.model_copy(update={"budget": task.budget.charge(
            max(remaining.tokens, judge_usage.tokens),
            max(remaining.cost_usd, judge_usage.cost_usd),
            unconfirmed_tokens=remaining.unconfirmed_tokens,
            unconfirmed_cost_usd=remaining.unconfirmed_cost_usd,
        )})
        updates.append(judged)
        if evaluation is not None:
            evaluations.append(evaluation)
        total_tokens += judge_usage.tokens
        total_cost += judge_usage.cost_usd

    return {
        "plan": updates,
        "evaluations": evaluations,
        "usage": {"tokens": total_tokens, "cost_usd": total_cost},
        "phase": WorkflowPhase.EVALUATING,
    }


def build_execution_nodes(deps: NodeDependencies) -> dict[str, Any]:
    """Amarra as etapas da fase às dependências, nos nomes do workflow."""

    def route_to_execution(state: WorkflowState) -> list[Send] | str:
        return route_ready_tasks(deps, state)

    async def execute_task(payload: ExecutionPayload) -> dict[str, Any]:
        return await run_task(deps, payload)

    async def evaluate_results(state: WorkflowState) -> dict[str, Any]:
        return await consolidate_results(deps, state)

    return {
        "route_to_execution": route_to_execution,
        "execute_task": execute_task,
        "evaluate_results": evaluate_results,
    }
