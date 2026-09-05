"""Fase de execução: fan-out, worker por tarefa e consolidação no join.

FAN-OUT VIA Send(): route_to_execution emite um Send por tarefa em
state.ready_tasks. Cada worker executa E JULGA a própria tarefa
(julgamento incremental — a rápida não espera a lenta) e retorna APENAS as
tarefas que tocou; o reducer merge_tasks_by_id consolida sem colisão.

TIMEOUT NO WORKER: asyncio.wait_for com task.timeout_seconds (regra 4).
Estouro vira FAILED/ESCALATED — nunca exceção não tratada no grafo.
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
    ExecutionPayload,
    Judge,
    JudgingOutcome,
    NodeDependencies,
    UsageReport,
)
from app.graph.state import WorkflowPhase, WorkflowState
from app.infrastructure.llm_budget import (
    CallBudget, active_call_budget, call_budget_scope,
)
from app.infrastructure.tracing import current_trace_id
from app.models.build_execution import BuildOutcome, BuildRunResult
from app.models.factory import FactoryStage
from app.models.task import AgentTask, EvaluationResult, TaskAttempt, TaskStatus


def build_execution_nodes(deps: NodeDependencies) -> dict[str, Any]:
    async def record_build_report(
        payload: ExecutionPayload, report: BuildRunResult
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
        if not selected_tasks:
            return "human_gate"
        token_allowance = max(
            0, state.budget.max_tokens - int(state.usage.get("tokens", 0))
            - int(state.usage.get("unconfirmed_tokens", 0)),
        ) // len(selected_tasks)
        cost_allowance = max(
            0.0, state.budget.max_cost_usd - state.usage.get("cost_usd", 0.0)
            - state.usage.get("unconfirmed_cost_usd", 0.0),
        ) / len(selected_tasks)
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

    async def run_build_validation(
        payload: ExecutionPayload,
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
                await record_build_report(payload, exc.report)
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
        await record_build_report(payload, build_report)
        return build_report

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

    async def execute_task(payload: ExecutionPayload) -> dict[str, Any]:
        task = payload.task
        executor = deps.active_registry(payload.workspace).select(task)
        started = datetime.now(timezone.utc)
        attempt_number = task.attempt_count + 1

        if task.budget.exhausted:
            return failed_attempt(
                payload,
                attempt_number=attempt_number,
                started=started,
                reason="budget da tarefa esgotado antes da execução",
            )

        try:
            outcome = await asyncio.wait_for(
                executor.execute(task, payload.context),
                timeout=task.timeout_seconds,
            )
            result = outcome.get("result")
            build_report: BuildRunResult | None = None
            if payload.workspace is not None and payload.build_strategy is not None:
                build_report = await run_build_validation(payload)
                result = attach_build_report(result, build_report)
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
                trace_id=current_trace_id(),
                operational_summary=attempt_operational_summary(result),
                factory_stage=(
                    FactoryStage.VALIDATION
                    if build_report is not None
                    else payload.factory_stage
                ),
                build_strategy=payload.build_strategy,
                build_validation=build_report,
            )
            updated = task.model_copy(
                update={
                    "status": TaskStatus.RUNNING,
                    "result": result,
                    "attempts": [*task.attempts, attempt],
                    "budget": charged_budget,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            if budget_exceeded:
                updated = updated.model_copy(
                    update={"status": updated.next_status_after_failure()}
                )
                return {
                    "plan": [updated],
                    "usage": {"tokens": tokens, "cost_usd": cost},
                }
            # Julgamento incremental: a tarefa é julgada no próprio branch,
            # em paralelo com as demais — a rápida não espera a lenta para
            # receber veredito. O judge_router segue decidindo no join,
            # sobre o estado consolidado.
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

        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            reason = (
                f"timeout após {task.timeout_seconds}s"
                if isinstance(exc, asyncio.TimeoutError)
                else f"{type(exc).__name__}: {exc}"
            )
            return failed_attempt(
                payload, attempt_number=attempt_number, started=started, reason=reason
            )

    async def evaluate_results(state: WorkflowState) -> dict[str, Any]:
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

    return {
        "route_to_execution": route_to_execution,
        "execute_task": execute_task,
        "evaluate_results": evaluate_results,
    }
