"""Nós do workflow Forgehand.

Decisões estruturais:

1. INJEÇÃO DE DEPENDÊNCIA: nenhum nó importa provider de LLM. Planner,
   executores e judge chegam por protocolos — os nós são testáveis com fakes
   e o acoplamento a fornecedor fica confinado em app/providers (regra 1).

2. FAN-OUT VIA Send(): o roteador `route_to_execution` emite um Send por
   tarefa em state.ready_tasks. Cada worker executa E JULGA a própria
   tarefa (julgamento incremental — a rápida não espera a lenta) e retorna
   APENAS as tarefas que tocou; o reducer merge_tasks_by_id consolida sem
   colisão. O veredito agregado continua no join (judge_router).

3. TIMEOUT NO WORKER: asyncio.wait_for com task.timeout_seconds (regra 4).
   Estouro vira FAILED/ESCALATED — nunca exceção não tratada no grafo.

4. GATE HUMANO: human_gate usa interrupt(). O grafo pausa no checkpointer
   e só continua com Command(resume={...}) — decisão humana rastreável.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Protocol, cast
from uuid import UUID

from langgraph.types import Send, interrupt
from pydantic import BaseModel, Field

from app.agents.validation import format_validation_feedback
from app.factory.architecture import architecture_feedback
from app.factory.acceptance import acceptance_feedback, acceptance_verified
from app.factory.delivery import factory_delivery_config, factory_ready_for_review
from app.factory.sandbox import BuildRunCancelled
from app.graph.state import (
    DeliveryConfig,
    DeliveryResult,
    WorkflowPhase,
    WorkflowState,
)
from app.infrastructure.tracing import current_trace_id
from app.factory.workspace import WorkspaceManager, WorkspaceRuntimeFactory
from app.models.build import BuildProfile
from app.models.build_execution import BuildOutcome, BuildRunResult
from app.models.factory import (
    BuildProfileSelection,
    FactoryStage,
    WorkOrder,
    WorkspaceLease,
)
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
    ) -> DeliveryResult: ...


# --------------------------------------------------------------------------
# Fábrica de nós
# --------------------------------------------------------------------------


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
    # import local: scm importa state (modelos de entrega); nodes não deve
    # depender de infraestrutura além desta função pura de coleta.
    from app.infrastructure.scm import (
        collect_publishable_changes,
        task_publishes_changes,
    )

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
            "build_validation": workspace.get("build_validation"),
        }

    def build_report_from_task(task: AgentTask) -> BuildRunResult | None:
        if task.attempts and task.attempts[-1].build_validation is not None:
            return task.attempts[-1].build_validation
        if not isinstance(task.result, dict):
            return None
        workspace = task.result.get("workspace")
        if not isinstance(workspace, dict) or workspace.get("build_validation") is None:
            return None
        try:
            return BuildRunResult.model_validate(workspace["build_validation"])
        except ValueError:
            return None

    def build_feedback(report: BuildRunResult) -> list[dict[str, Any]]:
        feedback = [
            {
                "name": f"sandbox:{phase.phase.value}",
                "passed": phase.outcome == BuildOutcome.SUCCESS,
                "command": json.dumps(list(phase.command), ensure_ascii=False),
                "exit_code": phase.exit_code,
                "details": phase.error_code or phase.outcome.value,
                "stdout": phase.stdout,
                "stderr": phase.stderr,
                "duration_seconds": phase.duration_seconds,
                "output_truncated": phase.output_truncated,
            }
            for phase in report.phases
        ]
        if not feedback and report.outcome != BuildOutcome.SUCCESS:
            feedback.append(
                {
                    "name": "sandbox",
                    "passed": False,
                    "command": "",
                    "exit_code": None,
                    "details": report.error_code or report.outcome.value,
                    "stdout": "",
                    "stderr": "",
                    "duration_seconds": 0.0,
                    "output_truncated": False,
                }
            )
        if report.architecture is not None:
            feedback.extend(architecture_feedback(report.architecture))
        if report.acceptance is not None:
            feedback.extend(acceptance_feedback(report.acceptance))
        return feedback

    def attach_build_report(
        result: dict[str, Any] | None, report: BuildRunResult
    ) -> dict[str, Any]:
        attached = dict(result) if isinstance(result, dict) else {}
        workspace = attached.get("workspace")
        workspace = dict(workspace) if isinstance(workspace, dict) else {}
        workspace["build_validation"] = report.model_dump(mode="json")
        existing = workspace.get("command_feedback")
        feedback = list(existing) if isinstance(existing, list) else []
        feedback.extend(build_feedback(report))
        workspace["command_feedback"] = feedback
        attached["workspace"] = workspace
        return attached

    async def record_build_report(
        payload: ExecutionPayload, report: BuildRunResult
    ) -> None:
        if (
            build_audit_recorder is None
            or payload.workspace is None
            or payload.build_strategy is None
        ):
            return
        await build_audit_recorder(
            project_id=payload.project_id,
            client_id=payload.owner_client_id,
            lease=payload.workspace,
            selection=payload.build_strategy,
            report=report,
        )

    def apply_build_veto(
        evaluation: EvaluationResult, report: BuildRunResult | None
    ) -> EvaluationResult:
        if report is None:
            return evaluation
        validated_by = list(dict.fromkeys([*evaluation.validated_by, "sandbox"]))
        if report.architecture is not None:
            validated_by = list(dict.fromkeys([*validated_by, "architecture"]))
        if report.acceptance is not None:
            validated_by = list(dict.fromkeys([*validated_by, "independent_acceptance"]))
        phase_by_name = {phase.phase.value: phase for phase in report.phases}

        def signal(name: str, current: bool | None) -> bool | None:
            phase = phase_by_name.get(name)
            if phase is None:
                return current
            passed = phase.outcome == BuildOutcome.SUCCESS
            return passed if current is None else current and passed

        updates: dict[str, Any] = {
            "validated_by": validated_by,
            "tests_passed": signal("test", evaluation.tests_passed),
            "lint_passed": signal("lint", evaluation.lint_passed),
            "type_check_passed": signal("types", evaluation.type_check_passed),
        }
        if report.outcome != BuildOutcome.SUCCESS or (
            report.architecture is not None and not report.architecture.passed
        ) or (
            report.acceptance is not None and not report.acceptance.passed
        ):
            failures = [
                f"[sandbox:{phase.phase.value}] "
                f"{phase.error_code or phase.outcome.value}"
                for phase in report.phases
                if phase.outcome != BuildOutcome.SUCCESS
            ] or [f"[sandbox] {report.error_code or report.outcome.value}"]
            if report.architecture is not None:
                failures.extend(
                    f"[architecture:{item.rule_id}] {item.path}:{item.line} → {item.dependency}; {item.remediation}"
                    for item in report.architecture.findings[:10]
                )
            if report.acceptance is not None:
                failures.extend(
                    f"[acceptance:{case.case_id}] {case.criterion}: saída ou execução não atende ao contrato."
                    for case in report.acceptance.cases if not case.passed
                )
            updates.update(
                approved=False,
                score=min(evaluation.score, 0.4),
                failures=[*evaluation.failures, *failures],
                required_changes=[
                    *evaluation.required_changes,
                    "As fases obrigatórias do perfil de build reprovaram. "
                    "Corrija a causa usando a evidência sanitizada abaixo:",
                    *failures,
                ],
            )
        return evaluation.model_copy(update=updates)

    def active_planner(state: WorkflowState) -> Planner:
        if state.workspace is not None and runtime_factory is not None:
            return cast(Planner, runtime_factory.build_planner(state.workspace))
        return planner

    def active_registry(lease: WorkspaceLease | None) -> ExecutorRegistry:
        if lease is not None and runtime_factory is not None:
            return cast(ExecutorRegistry, runtime_factory.build_registry(lease))
        return registry

    def active_judge(lease: WorkspaceLease | None) -> Judge:
        if lease is not None and runtime_factory is not None:
            return cast(Judge, runtime_factory.build_judge(lease))
        return judge

    async def provision_workspace(state: WorkflowState) -> dict[str, Any]:
        if state.work_order is None:
            return {"phase": WorkflowPhase.LOADING_CONTEXT}
        if workspace_manager is None or runtime_factory is None:
            return {
                "error": "factory runtime não configurado",
                "phase": WorkflowPhase.FAILED,
                "factory_stage": FactoryStage.PROVISIONING,
            }
        try:
            lease = (
                await workspace_manager.reconstruct(state.workspace)
                if state.workspace is not None
                else await workspace_manager.provision(
                    state.workflow_id, state.work_order
                )
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "error": f"workspace provisioning failed: {type(exc).__name__}: {exc}",
                "phase": WorkflowPhase.FAILED,
                "factory_stage": FactoryStage.PROVISIONING,
            }
        return {
            "workspace": lease,
            "phase": WorkflowPhase.LOADING_CONTEXT,
            "factory_stage": FactoryStage.PROVISIONING,
        }

    def provision_router(state: WorkflowState) -> str:
        return (
            "persist_memory" if state.phase == WorkflowPhase.FAILED else "load_context"
        )

    async def select_build_strategy(state: WorkflowState) -> dict[str, Any]:
        """Persiste a política selecionada antes de ler ou executar o projeto."""
        if state.work_order is None:
            return {"phase": WorkflowPhase.LOADING_CONTEXT}
        if state.workspace is None:
            return {
                "error": "build strategy selection requires a workspace lease",
                "phase": WorkflowPhase.FAILED,
                "factory_stage": FactoryStage.STRATEGY_SELECTION,
            }

        current = state.build_strategy
        try:
            if build_strategy_selector is None:
                raise ValueError("nenhum registro de perfis foi configurado.")
            if current is not None and current.selection_reason != "unsupported":
                # Retomadas só reutilizam a seleção se a definição administrada
                # ainda corresponder ao fingerprint persistido.
                build_strategy_selector.profile_for(current)
                if current.acceptance_digest is not None and current.acceptance_criteria != state.work_order.acceptance_criteria:
                    raise ValueError("Critérios de aceitação mudaram após seleção.")
                selection = current
            else:
                selection = build_strategy_selector.select(
                    state.work_order, state.workspace
                )
        except (TypeError, ValueError) as exc:
            selection = BuildProfileSelection(
                requested_profile=state.work_order.build_profile.requested_profile,
                selection_reason="unsupported",
                unsupported_reason=f"seleção persistida inválida: {exc}",
            )

        if strategy_audit_recorder is not None:
            await strategy_audit_recorder(
                workflow_id=state.workflow_id,
                project_id=state.project_id,
                client_id=state.owner_client_id,
                repository=state.work_order.repository.full_name,
                selection=selection,
            )

        unsupported = selection.selection_reason == "unsupported"
        return {
            "build_strategy": selection,
            "factory_stage": FactoryStage.STRATEGY_SELECTION,
            "phase": (
                WorkflowPhase.UNSUPPORTED_BUILD_STRATEGY
                if unsupported
                else WorkflowPhase.LOADING_CONTEXT
            ),
            "error": (
                f"unsupported_build_strategy: {selection.unsupported_reason}"
                if unsupported
                else None
            ),
        }

    def strategy_router(state: WorkflowState) -> str:
        if state.phase == WorkflowPhase.FAILED:
            return "persist_memory"
        if state.work_order is None:
            return "load_context"
        if (
            state.build_strategy is None
            or state.build_strategy.selection_reason == "unsupported"
        ):
            return "human_gate"
        return "load_context"

    async def load_context(state: WorkflowState) -> dict[str, Any]:
        if state.workspace is not None and runtime_factory is not None:
            project_context_loader = getattr(memory, "load_project_context", None)
            ctx = (
                await project_context_loader(state.project_id, state.request)
                if callable(project_context_loader)
                else await memory.load_context(state.project_id, state.request)
            )
            ctx = dict(ctx)
            ctx.pop("repository_grounding", None)
            ctx["repository_grounding"] = runtime_factory.build_grounding(
                state.workspace, state.request
            )
        else:
            ctx = await memory.load_context(state.project_id, state.request)
        ctx = dict(ctx)
        ctx.pop("architecture_policy_guidance", None)
        ctx.pop("acceptance_policy_guidance", None)
        if state.build_strategy is not None and build_strategy_selector is not None:
            profile = build_strategy_selector.profile_for(state.build_strategy)
            if profile.acceptance is not None:
                ctx["acceptance_policy_guidance"] = (
                    "Aceitação independente definida pelo operador; implemente o comportamento, não altere os critérios.\n"
                    + "\n".join(f"{case.id}: {case.criterion}" for case in profile.acceptance.cases)
                )
            if profile.architecture is not None:
                ctx["architecture_policy_guidance"] = (
                    "Política de arquitetura aprovada pelo operador. Não altere nem contorne as regras.\n"
                    + "\n".join(
                        f"{rule.id}: {rule.source} não pode importar {', '.join(rule.forbidden)}. {rule.remediation}"
                        for rule in profile.architecture.rules
                    )
                )
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
        raw = await active_planner(state).create_plan(state.request, state.context)
        if isinstance(raw, PlanningOutcome):
            plan = raw.plan
            usage = raw.usage.model_dump()
        else:  # compatibilidade com implementações do protocolo existentes
            plan = raw
            usage = {"tokens": 0, "cost_usd": 0.0}
        if not plan:
            raise ValueError("Planner retornou plano vazio.")
        update: dict[str, Any] = {
            "plan": plan,
            "usage": usage,
            "phase": WorkflowPhase.EXECUTING,
        }
        if state.work_order is not None:
            update["factory_stage"] = FactoryStage.IMPLEMENTATION
        return update

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
        for t in dispatchable:
            selected_registry = active_registry(state.workspace)
            dispatch_policy = getattr(selected_registry, "dispatch_policy", None)
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
                    ExecutionPayload(
                        task=t,
                        project_id=state.project_id,
                        context=ctx,
                        workspace=state.workspace,
                        factory_stage=state.factory_stage,
                        build_strategy=state.build_strategy,
                        owner_client_id=state.owner_client_id,
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
        judged = task.model_copy(
            update={"status": new_status, "updated_at": datetime.now(timezone.utc)}
        )
        return judged, evaluation, judge_usage

    async def execute_task(payload: ExecutionPayload) -> dict[str, Any]:
        task = payload.task
        executor = active_registry(payload.workspace).select(task)
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
                trace_id=current_trace_id(),
                factory_stage=payload.factory_stage,
                build_strategy=payload.build_strategy,
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
            result = outcome.get("result")
            build_report: BuildRunResult | None = None
            if payload.workspace is not None and payload.build_strategy is not None:
                if build_runner is None:
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
                                await build_runner.run(
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
                active_judge(payload.workspace),
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
            attempt = TaskAttempt(
                attempt_number=attempt_number,
                agent_name=task.assigned_agent or "unknown",
                model="unknown",
                started_at=started,
                finished_at=datetime.now(timezone.utc),
                outcome=TaskStatus.FAILED,
                failure_reason=reason,
                trace_id=current_trace_id(),
                factory_stage=payload.factory_stage,
                build_strategy=payload.build_strategy,
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
            judged, evaluation, judge_usage = await judge_task(
                task, state.context, active_judge(state.workspace)
            )
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
        report = next(
            (
                candidate
                for task in reversed(state.plan)
                if (candidate := build_report_from_task(task)) is not None
            ),
            None,
        )
        if report is not None:
            parts.append(_build_validation_section(report))
        return {
            "final_output": "\n".join(parts),
            "phase": WorkflowPhase.SYNTHESIZING,
        }

    async def abort(state: WorkflowState) -> dict[str, Any]:
        return {
            "final_output": f"Workflow abortado por decisão humana. Uso: {state.usage}",
            "phase": WorkflowPhase.FAILED,
        }

    # ------------------------------------------------------------------
    # Entrega: PR + CI como sinal objetivo (fecha o ciclo até o PR verde)
    # ------------------------------------------------------------------
    def delivery_router(state: WorkflowState) -> str:
        """Após synthesize: publica se o workflow tem destino configurado."""
        if state.work_order is not None:
            return "publish_delivery"
        if state.delivery is None or delivery is None:
            return "persist_memory"
        if state.phase == WorkflowPhase.FAILED:
            return "persist_memory"
        return "publish_delivery"

    async def publish_delivery(state: WorkflowState) -> dict[str, Any]:
        previous = state.delivery_result
        attempts = (previous.attempts if previous is not None else 0) + 1
        config = state.delivery
        if state.work_order is not None:
            try:
                config = factory_delivery_config(state)
                if delivery is None:
                    raise ValueError("factory_delivery_publisher_unavailable")
            except ValueError as exc:
                return {
                    "delivery_result": DeliveryResult(
                        ci_state="error", error=str(exc), attempts=attempts
                    ),
                    "factory_stage": FactoryStage.DELIVERY,
                    "phase": WorkflowPhase.DELIVERING,
                }
        assert config is not None and delivery is not None
        # Só tarefas APROVADAS publicam — parcial aceito por humano inclusive.
        completed = state.completed_tasks
        files, deletions = collect_publishable_changes(
            [{"result": t.result} for t in completed]
        )
        if not files and not deletions:
            if previous is not None and previous.url:
                # Nada novo aprovado (ex.: parcial aceito após CI vermelho):
                # a última publicação continua sendo a verdade sobre o PR.
                result = previous.model_copy(
                    update={
                        "note": "nenhuma tarefa aprovada nova; última publicação mantida"
                    }
                )
            else:
                result = DeliveryResult(
                    ci_state="skipped",
                    note="nenhum arquivo publicável nas tarefas aprovadas",
                    attempts=attempts,
                )
            skipped_update: dict[str, Any] = {
                "delivery_result": result,
                "phase": WorkflowPhase.DELIVERING,
                "final_output": _with_delivery_section(state.final_output, result),
            }
            if state.work_order is not None:
                skipped_update["factory_stage"] = FactoryStage.DELIVERY
            return skipped_update

        summary = (
            f"{state.project_id} — iteração {state.iteration}, {len(files)} arquivo(s)"
        )
        if deletions:
            summary += f", {len(deletions)} remoção(ões)"
        if config.pinned_base_sha is not None:
            summary += f"; base={config.pinned_base_sha}; revisão humana obrigatória"
        report = next(
            (
                candidate
                for task in reversed(completed)
                if (candidate := build_report_from_task(task)) is not None
            ),
            None,
        )
        if report is not None:
            phases = ",".join(
                f"{phase.phase.value}:{phase.outcome.value}" for phase in report.phases
            )
            summary += f"; sandbox={report.outcome.value}; phases={phases or '-'}"
        result = await delivery.publish(
            config=config,
            workflow_id=state.workflow_id,
            project_id=state.project_id,
            files=files,
            deletions=deletions,
            summary=summary,
        )
        result = result.model_copy(update={"attempts": attempts})

        updates: dict[str, Any] = {
            "delivery_result": result,
            "phase": WorkflowPhase.DELIVERING,
            "final_output": _with_delivery_section(state.final_output, result),
        }
        if state.work_order is not None:
            updates["factory_stage"] = FactoryStage.DELIVERY
        if not result.ci_failed or state.human_decision == "accept_partial":
            return updates

        # CI reprovou o commit publicado: sinal objetivo. Reabre as tarefas
        # que contribuíram com arquivos, com as falhas como required_changes,
        # e deixa o judge_router/replan decidirem (iterações continuam a
        # limitar o ciclo; esgotadas, cai no gate humano).
        now = datetime.now(timezone.utc)
        reopen_reason = f"ci:{result.commit_sha or attempts}"
        reopened: list[AgentTask] = []
        evaluations: list[EvaluationResult] = []
        feedback = result.failures[:15] or ["CI reprovou o commit publicado."]
        responsible: set[UUID] | None = None
        if state.work_order is not None:
            # A última tarefa que escreveu o arquivo é responsável pelo seu
            # conteúdo publicado. Sem atribuição objetiva, pede decisão humana.
            owners: dict[str, UUID] = {}
            for owner_task in completed:
                owned_files, owned_deletions = collect_publishable_changes(
                    [{"result": owner_task.result}]
                )
                for path in [item["path"] for item in owned_files] + owned_deletions:
                    owners[path] = owner_task.id
            responsible = {
                owners[path] for path in result.failure_paths if path in owners
            }
            if any(path not in owners for path in result.failure_paths):
                responsible = set()
        for task in completed:
            if not task_publishes_changes(task.result):
                continue
            if responsible is not None and task.id not in responsible:
                continue
            evaluations.append(
                EvaluationResult(
                    task_id=task.id,
                    approved=False,
                    score=0.0,
                    criteria_scores={c.text: 0.0 for c in task.acceptance_criteria},
                    failures=[f"[ci] {line}" for line in feedback],
                    required_changes=[
                        "O CI do pull request reprovou o código publicado. "
                        "Corrija as falhas abaixo mantendo os critérios já atendidos:",
                        *[f"- {line}" for line in feedback],
                    ],
                    tests_passed=False,
                    validated_by=["ci"],
                )
            )
            reopened.append(
                task.model_copy(
                    update={
                        "status": task.next_status_after_failure(),
                        "reopen_reason": reopen_reason,
                        "updated_at": now,
                    }
                )
            )
        updates["plan"] = reopened
        updates["evaluations"] = evaluations
        return updates

    def delivery_result_router(state: WorkflowState) -> str:
        """Após publish_delivery: CI verde/sem CI → persiste; CI vermelho →
        replan enquanto houver iteração, senão gate humano. Parcial aceito
        por humano nunca volta ao ciclo."""
        result = state.delivery_result
        if state.work_order is not None:
            if factory_ready_for_review(state):
                return "persist_memory"
            if result is None or not result.ci_failed:
                return "human_gate"
        if result is None or not result.ci_failed:
            return "persist_memory"
        if state.human_decision == "accept_partial":
            return "persist_memory"
        if state.escalated_tasks or state.budget_exhausted:
            return "human_gate"
        if state.tasks_needing_replan:
            return "human_gate" if state.iterations_exhausted else "replan"
        return "human_gate" if state.work_order is not None else "persist_memory"

    async def persist_memory(state: WorkflowState) -> dict[str, Any]:
        if state.work_order is not None:
            ready = state.phase != WorkflowPhase.FAILED and factory_ready_for_review(
                state
            )
            terminal_state = state.model_copy(
                update={
                    "phase": (
                        WorkflowPhase.READY_FOR_HUMAN_REVIEW
                        if ready
                        else WorkflowPhase.FAILED
                    ),
                    "factory_stage": (
                        FactoryStage.READY_FOR_HUMAN_REVIEW
                        if ready
                        else state.factory_stage
                    ),
                    "final_output": (
                        (state.final_output or "")
                        + "\n\nPronto para revisão humana. O Forgehand não faz merge."
                        if ready
                        else state.final_output
                    ),
                }
            )
            await memory.persist(terminal_state)
            return {
                "phase": terminal_state.phase,
                "factory_stage": terminal_state.factory_stage,
                "final_output": terminal_state.final_output,
            }
        await memory.persist(state)
        terminal = (
            WorkflowPhase.FAILED
            if state.phase == WorkflowPhase.FAILED
            else WorkflowPhase.COMPLETED
        )
        return {"phase": terminal}

    return {
        "provision_workspace": provision_workspace,
        "provision_router": provision_router,
        "select_build_strategy": select_build_strategy,
        "strategy_router": strategy_router,
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
        "delivery_router": delivery_router,
        "publish_delivery": publish_delivery,
        "delivery_result_router": delivery_result_router,
        "persist_memory": persist_memory,
    }


def _with_delivery_section(final_output: str | None, result: DeliveryResult) -> str:
    lines = [final_output or "", "", "## Entrega"]
    if result.url:
        lines.append(f"Pull request: {result.url} (branch `{result.branch}`)")
    if result.commit_sha:
        lines.append(f"Commit: `{result.commit_sha[:12]}`")
    lines.append(f"CI: {result.ci_state}")
    if result.error:
        lines.append(f"Erro: {result.error}")
    for line in result.failures[:10]:
        lines.append(f"- {line}")
    return "\n".join(lines).strip()


def _build_validation_section(report: BuildRunResult) -> str:
    lines = ["## Validação em sandbox", f"Resultado: {report.outcome.value}"]
    if report.acceptance is None:
        lines.append("Aceitação independente: sem evidência; testes do repositório não comprovam os requisitos por si só.")
    else:
        acceptance = report.acceptance
        lines.append(
            f"Aceitação independente: {'aprovada' if acceptance.passed else 'reprovada'}; "
            f"casos={len(acceptance.cases)}; critérios declarados={len(set(acceptance.required_criteria))}; "
            f"suite={acceptance.suite_digest}"
        )
        for case in acceptance.cases:
            lines.append(f"- {case.case_id}: {'passou' if case.passed else 'falhou'} — {case.criterion}")
    if report.architecture is not None:
        architecture = report.architecture
        lines.append(
            f"Arquitetura: {'aprovada' if architecture.passed else 'reprovada'}; arquivos={architecture.files_checked}"
        )
        for finding in architecture.findings[:10]:
            lines.append(
                f"- {finding.rule_id}: {finding.path}:{finding.line} → {finding.dependency}; {finding.remediation}"
            )
    for phase in report.phases:
        detail = phase.error_code or phase.outcome.value
        lines.append(
            f"- {phase.phase.value}: {phase.outcome.value} "
            f"({phase.duration_seconds:.3f}s; {detail})"
        )
    if report.error_code and not report.phases:
        lines.append(f"- erro: {report.error_code}")
    return "\n".join(lines)
