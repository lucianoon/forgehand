"""Fase de entrega: PR + CI como sinal objetivo e persistência de memória.

Fecha o ciclo até o PR verde: CI vermelho reabre só as tarefas que
contribuíram com arquivos e devolve ao replan; esgotadas as iterações, cai
no gate humano. Em factory mode o estado terminal é READY_FOR_HUMAN_REVIEW —
o Forgehand nunca faz merge.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.factory.delivery import factory_delivery_config, factory_ready_for_review
from app.graph.build_evidence import latest_build_report, with_delivery_section
from app.graph.contracts import NodeDependencies
from app.graph.state import DeliveryResult, WorkflowPhase, WorkflowState
from app.models.factory import FactoryStage
from app.models.task import AgentTask, EvaluationResult


def criteria_details(state: WorkflowState, completed: list[AgentTask]) -> str:
    """Tabela Markdown com o veredito por critério das tarefas publicadas,
    para o corpo do PR: o revisor vê o que foi provado por código e o que
    ficou com o judge LLM."""
    latest: dict[UUID, EvaluationResult] = {}
    for evaluation in state.evaluations:
        latest[evaluation.task_id] = evaluation
    lines = [
        "## Critérios verificados",
        "",
        "| Tarefa | Critério | Nota | Validado por |",
        "|---|---|---:|---|",
    ]
    rows = 0
    for task in completed:
        verdict = latest.get(task.id)
        if verdict is None:
            continue
        validated = ", ".join(verdict.validated_by) or "llm"
        for text, score in verdict.criteria_scores.items():
            lines.append(
                f"| {task.title[:60].replace('|', '/')} | {text[:80].replace('|', '/')} "
                f"| {score:.2f} | {validated} |"
            )
            rows += 1
    return "\n".join(lines) if rows else ""


def build_delivery_nodes(deps: NodeDependencies) -> dict[str, Any]:
    # import local: scm importa state (modelos de entrega); o grafo não deve
    # depender de infraestrutura além destas funções puras de coleta.
    from app.infrastructure.scm import (
        collect_publishable_changes,
        task_publishes_changes,
    )

    delivery = deps.delivery

    def delivery_router(state: WorkflowState) -> str:
        """Após synthesize: publica se o workflow tem destino configurado."""
        if state.work_order is not None:
            return "publish_delivery"
        if state.delivery is None or delivery is None:
            return "persist_memory"
        if state.phase == WorkflowPhase.FAILED:
            return "persist_memory"
        return "publish_delivery"

    def reopen_after_ci_failure(
        state: WorkflowState,
        completed: list[AgentTask],
        result: DeliveryResult,
        attempts: int,
    ) -> tuple[list[AgentTask], list[EvaluationResult]]:
        """CI reprovou o commit publicado: sinal objetivo. Reabre as tarefas
        que contribuíram com arquivos, com as falhas como required_changes,
        e deixa o judge_router/replan decidirem (iterações continuam a
        limitar o ciclo; esgotadas, cai no gate humano)."""
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
        return reopened, evaluations

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
                "final_output": with_delivery_section(state.final_output, result),
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
        report = latest_build_report(completed)
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
            details=criteria_details(state, completed),
        )
        result = result.model_copy(update={"attempts": attempts})

        updates: dict[str, Any] = {
            "delivery_result": result,
            "phase": WorkflowPhase.DELIVERING,
            "final_output": with_delivery_section(state.final_output, result),
        }
        if state.work_order is not None:
            updates["factory_stage"] = FactoryStage.DELIVERY
        if not result.ci_failed or state.human_decision == "accept_partial":
            return updates

        reopened, evaluations = reopen_after_ci_failure(
            state, completed, result, attempts
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
            await deps.memory.persist(terminal_state)
            return {
                "phase": terminal_state.phase,
                "factory_stage": terminal_state.factory_stage,
                "final_output": terminal_state.final_output,
            }
        await deps.memory.persist(state)
        terminal = (
            WorkflowPhase.FAILED
            if state.phase == WorkflowPhase.FAILED
            else WorkflowPhase.COMPLETED
        )
        return {"phase": terminal}

    return {
        "delivery_router": delivery_router,
        "publish_delivery": publish_delivery,
        "delivery_result_router": delivery_result_router,
        "persist_memory": persist_memory,
    }
