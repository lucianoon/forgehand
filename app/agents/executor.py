"""Executor — executa UMA tarefa e devolve artefatos estruturados.

O contrato de retorno é o que app.graph.nodes.execute_task espera:
{"result", "agent", "model", "tokens", "cost_usd"} — usage e custo saem do
CompletionResult, fechando o circuito TaskBudget → WorkflowBudget (regra 9).
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field
from app.agents.grounding import format_repository_grounding, grounding_required
from app.agents.validation import format_validation_feedback

from app.models.task import AgentTask, Capability
from app.providers.base import CompletionRequest, Message
from app.providers.registry import ModelTier, ProviderRouter

SYSTEM_PROMPT = """Você é um executor especializado do Forgehand \
({capability}).

Execute EXATAMENTE a tarefa descrita. Regras:
- produza artefatos completos e funcionais, nunca trechos com "...";
- cada arquivo em files com path relativo à raiz do projeto;
- os acceptance_criteria são o contrato: o judge vai reprovar qualquer \
critério não atendido;
- se a descrição contém "Correções exigidas pelo judge", trate cada \
correção como obrigatória;
- em notes, registre decisões técnicas relevantes e pressupostos assumidos;
- quando a tarefa pedir análise, riscos ou priorização baseada em evidências, \
  associe cada afirmação à evidência no próprio texto usando `[evidence_id]` e \
  explique objetivamente o impacto e a probabilidade que sustentam a prioridade;
- quando receber resultados de dependências, reutilize os fatos, arquivos e \
  citations concretos desses resultados; não faça apenas uma referência genérica \
  à tarefa anterior;
- se houver grounding do repositório, use SOMENTE as evidências fornecidas e \
  devolva `citations` com os evidence_ids usados para sustentar o resultado."""


class FileArtifact(BaseModel):
    path: str
    content: str


class ExecutionOutput(BaseModel):
    summary: str = Field(description="O que foi feito, em 2-4 frases.")
    files: list[FileArtifact] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


class ExecutionStrategy(BaseModel):
    apply_files: bool = True
    run_objective_validation: bool = True
    allow_autocorrect: bool = True


class WorkspaceRuntime(Protocol):
    async def apply(
        self,
        task: AgentTask,
        result_payload: dict[str, Any],
        strategy: ExecutionStrategy | None = None,
    ) -> dict[str, Any]: ...


class LLMExecutor:
    """Implementa o protocolo Executor de app.graph.nodes."""

    def __init__(
        self,
        router: ProviderRouter,
        agent_name: str,
        tier: ModelTier = ModelTier.STANDARD,
        workspace_runtime: WorkspaceRuntime | None = None,
        max_autocorrect_rounds: int = 0,
        execution_strategies: dict[Capability, ExecutionStrategy] | None = None,
    ):
        self._router = router
        self.agent_name = agent_name
        self.tier = tier
        self._workspace_runtime = workspace_runtime
        self._max_autocorrect_rounds = max(0, max_autocorrect_rounds)
        self._execution_strategies = execution_strategies or {}

    async def execute(self, task: AgentTask, context: dict[str, Any]) -> dict[str, Any]:
        strategy = self._execution_strategies.get(task.capability, ExecutionStrategy())
        previous_feedback = self._previous_attempt_feedback(task)
        current_iteration_feedback = ""
        total_tokens = 0
        total_cost = 0.0
        last_model = "unknown"
        payload: dict[str, Any] = {}
        autocorrect_iterations: list[dict[str, Any]] = []
        stopped_reason = "completed_without_runtime"
        max_rounds = self._max_autocorrect_rounds if strategy.allow_autocorrect else 0

        for iteration_index in range(max_rounds + 1):
            result = await self._router.complete(
                self.tier,
                CompletionRequest(
                    model="",
                    system=SYSTEM_PROMPT.format(capability=task.capability.value),
                    messages=[
                        Message(
                            role="user",
                            content=self._build_user_content(
                                task,
                                context,
                                previous_feedback=previous_feedback,
                                current_iteration_feedback=current_iteration_feedback,
                            ),
                        )
                    ],
                    response_schema=ExecutionOutput,
                    max_tokens=max(
                        1,
                        min(
                            16384,
                            task.budget.max_tokens - task.budget.consumed_tokens,
                        ),
                    ),
                ),
            )
            total_tokens += result.usage.total_tokens
            total_cost += result.cost_usd
            last_model = result.model
            output = result.parse_as(ExecutionOutput)
            payload = output.model_dump(mode="json")
            self._ensure_grounded_citations(task, context, payload)
            if self._workspace_runtime is not None:
                payload.update(
                    await self._workspace_runtime.apply(task, payload, strategy)
                )

            iteration_record = self._build_autocorrect_iteration(
                iteration_number=iteration_index + 1,
                payload=payload,
            )
            autocorrect_iterations.append(iteration_record)

            failed_feedback = self._failed_command_feedback(payload)
            can_retry = (
                self._workspace_runtime is not None
                and bool(failed_feedback)
                and iteration_index < max_rounds
            )
            if not can_retry:
                if self._workspace_runtime is None:
                    stopped_reason = "workspace_runtime_disabled"
                elif not failed_feedback:
                    stopped_reason = "checks_passed_or_skipped"
                elif not strategy.allow_autocorrect:
                    stopped_reason = "autocorrect_disabled_by_strategy"
                else:
                    stopped_reason = "max_autocorrect_rounds_exhausted"
                break
            current_iteration_feedback = self._feedback_from_payload(payload)

        workspace = payload.get("workspace")
        if isinstance(workspace, dict):
            workspace["strategy"] = strategy.model_dump(mode="json")
            workspace["autocorrect"] = {
                "enabled": max_rounds > 0,
                "max_rounds": max_rounds,
                "iterations": autocorrect_iterations,
                "stopped_reason": stopped_reason,
                "total_iterations": len(autocorrect_iterations),
            }
        return {
            "result": payload,
            "agent": self.agent_name,
            "model": last_model,
            "tokens": total_tokens,
            "cost_usd": total_cost,
        }

    def _build_user_content(
        self,
        task: AgentTask,
        context: dict[str, Any],
        *,
        previous_feedback: str,
        current_iteration_feedback: str,
    ) -> str:
        criteria = "\n".join(f"- {c}" for c in task.acceptance_criteria)
        user_content = (
            f"Tarefa: {task.title}\n\n"
            f"Descrição:\n{task.description}\n\n"
            f"Critérios de aceitação:\n{criteria}"
        )
        if previous_feedback:
            user_content += (
                f"\n\nFeedback operacional da tentativa anterior:\n{previous_feedback}"
            )
        if current_iteration_feedback:
            user_content += (
                "\n\nFeedback operacional da iteração interna anterior:\n"
                f"{current_iteration_feedback}"
            )
        dep_results = context.get("dependency_results")
        if dep_results:
            user_content += (
                "\n\nResultados das dependências (evidência de entrada):\n"
                f"{dep_results}\n"
                "Use os fatos e citations acima diretamente na entrega. Quando "
                "uma afirmação depender deles, indique o evidence_id inline, por "
                "exemplo `[E1]`."
            )
        grounding_block = format_repository_grounding(
            context,
            evidence_ids=context.get("task_evidence_ids") or task.evidence_ids,
            max_items=8,
        )
        if grounding_block:
            user_content += f"\n\n{grounding_block}"
        return user_content

    @staticmethod
    def _build_autocorrect_iteration(
        *,
        iteration_number: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        workspace = payload.get("workspace")
        if not isinstance(workspace, dict):
            return {"iteration": iteration_number, "failed_checks": []}
        command_feedback = workspace.get("command_feedback")
        feedback_items = command_feedback if isinstance(command_feedback, list) else []
        failed_checks = [
            item.get("name")
            for item in feedback_items
            if isinstance(item, dict) and item.get("passed") is False
        ]
        return {
            "iteration": iteration_number,
            "failed_checks": failed_checks,
            "applied_files": workspace.get("applied_files", []),
        }

    @staticmethod
    def _previous_attempt_feedback(task: AgentTask) -> str:
        if task.attempt_count == 0 or not isinstance(task.result, dict):
            return ""
        workspace = task.result.get("workspace")
        if not isinstance(workspace, dict):
            return ""
        return LLMExecutor._workspace_feedback_block(workspace)

    @staticmethod
    def _failed_command_feedback(payload: dict[str, Any]) -> list[dict[str, Any]]:
        workspace = payload.get("workspace")
        if not isinstance(workspace, dict):
            return []
        feedback = workspace.get("command_feedback")
        if not isinstance(feedback, list):
            return []
        return [
            item
            for item in feedback
            if isinstance(item, dict) and item.get("passed") is False
        ]

    @classmethod
    def _feedback_from_payload(cls, payload: dict[str, Any]) -> str:
        workspace = payload.get("workspace")
        if not isinstance(workspace, dict):
            return ""
        return cls._workspace_feedback_block(workspace)

    @staticmethod
    def _workspace_feedback_block(workspace: dict[str, Any]) -> str:
        lines: list[str] = []
        applied_files = workspace.get("applied_files")
        if isinstance(applied_files, list):
            valid_paths = [path for path in applied_files if isinstance(path, str)]
            if valid_paths:
                lines.append(f"Arquivos aplicados: {', '.join(valid_paths)}")
        feedback_block = format_validation_feedback(workspace.get("command_feedback"))
        if feedback_block:
            lines.append(feedback_block)
        return "\n".join(lines)

    @staticmethod
    def _ensure_grounded_citations(
        task: AgentTask,
        context: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        if not grounding_required(context):
            return
        evidence_ids = context.get("task_evidence_ids") or task.evidence_ids
        if not isinstance(evidence_ids, list):
            return
        grounded_ids = [item for item in evidence_ids if isinstance(item, str)]
        if grounded_ids:
            payload["citations"] = grounded_ids
