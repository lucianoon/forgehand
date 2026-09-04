"""Executor — executa UMA tarefa e devolve artefatos estruturados.

O contrato de retorno é o que app.graph.nodes.execute_task espera:
{"result", "agent", "model", "tokens", "cost_usd"} — usage e custo saem do
CompletionResult, fechando o circuito TaskBudget → WorkflowBudget (regra 9).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema
from app.agents.grounding import (
    build_grounding_prefix,
    format_evidence_focus,
    grounding_required,
)
from app.agents.tools import AgentTool, ToolLoop
from app.agents.web_tools import WEB_TOOL_GUIDANCE
from app.agents.hooks import ToolHookDispatcher
from app.agents.validation import format_validation_feedback

from app.models.task import AgentTask, Capability, format_criteria
from app.providers.base import CompletionRequest, Message
from app.providers.registry import ModelTier, ProviderRouter

SYSTEM_PROMPT = """Você é um executor especializado do Forgehand \
({capability}).

Execute EXATAMENTE a tarefa descrita. Regras:
- descreva as mudanças em `operations`, com path relativo à raiz do projeto:
  - arquivo NOVO: op=create com o conteúdo completo e funcional, nunca \
trechos com "...";
  - arquivo EXISTENTE: op=replace com `search` = o menor trecho que \
identifique unicamente o ponto a alterar, copiado LITERALMENTE das evidências \
do grounding (mesma indentação, mesmas linhas), e `replace` = o texto final \
desse trecho. Nunca reescreva um arquivo existente inteiro; use um replace por \
ponto alterado. Se o trecho aparece mais de uma vez, amplie `search` ou \
informe `occurrence`;
  - remover arquivo: op=delete;
- não edite trechos que você não viu nas evidências: se o arquivo alvo não \
está no grounding, registre isso em notes em vez de adivinhar o conteúdo;
- os acceptance_criteria são o contrato: o judge vai reprovar qualquer \
critério não atendido. Critérios marcados com [tipo] são verificados por \
código (testes, lint, arquivos criados/alterados, conteúdo, citations), sem \
margem de interpretação;
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

TOOLS_GUIDANCE = """

Ferramentas disponíveis: read_file, list_directory, search_repository e, \
quando oferecida, run_check. Use-as para ver o conteúdo EXATO de um arquivo \
antes de um op=replace, localizar usos de um símbolo e saber o que já falha. \
Explore o mínimo necessário (poucas chamadas, objetivas) e então emita a \
resposta final estruturada."""


class FileArtifact(BaseModel):
    """Formato legado (arquivo inteiro). Aceito na entrada por compatibilidade
    com checkpoints antigos; não vai mais no schema enviado ao modelo."""

    path: str
    content: str


class CreateFile(BaseModel):
    op: Literal["create"]
    path: str = Field(description="Path relativo à raiz do projeto.")
    content: str = Field(description="Conteúdo completo do arquivo novo.")


class ReplaceInFile(BaseModel):
    op: Literal["replace"]
    path: str = Field(description="Path relativo de um arquivo EXISTENTE.")
    search: str = Field(
        min_length=1,
        description=(
            "Trecho literal e único do arquivo atual (copiado das evidências, "
            "com a mesma indentação)."
        ),
    )
    replace: str = Field(description="Texto que substitui `search`.")
    occurrence: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Qual ocorrência substituir (1 = primeira) quando `search` aparece "
            "mais de uma vez. Omitido: `search` precisa ser único."
        ),
    )


class DeleteFile(BaseModel):
    op: Literal["delete"]
    path: str = Field(description="Path relativo do arquivo a remover.")


FileOperation = Annotated[
    CreateFile | ReplaceInFile | DeleteFile, Field(discriminator="op")
]


class ExecutionOutput(BaseModel):
    summary: str = Field(description="O que foi feito, em 2-4 frases.")
    operations: list[FileOperation] = Field(
        default_factory=list,
        description="Mudanças no workspace, na ordem em que devem ser aplicadas.",
    )
    # Legado: fora do JSON Schema (o modelo não vê), mas validado se vier em
    # payloads antigos. O workspace runtime converte para op=create.
    files: SkipJsonSchema[list[FileArtifact]] = Field(default_factory=list)
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
        tools: list[AgentTool] | None = None,
        max_tool_calls: int = 8,
        hooks: ToolHookDispatcher | None = None,
    ):
        self._router = router
        self.agent_name = agent_name
        self.tier = tier
        self._tool_loop = ToolLoop(
            router,
            tools,
            max_tool_calls=max_tool_calls,
            hooks=hooks,
            agent_name=agent_name,
        )
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
        cumulative_workspace: dict[str, Any] | None = None
        autocorrect_iterations: list[dict[str, Any]] = []
        stopped_reason = "completed_without_runtime"
        max_rounds = self._max_autocorrect_rounds if strategy.allow_autocorrect else 0

        cache_prefix = build_grounding_prefix(context)
        for iteration_index in range(max_rounds + 1):
            remaining_tokens = max(
                1,
                task.budget.max_tokens - task.budget.consumed_tokens - total_tokens,
            )
            loop_outcome = await self._tool_loop.run(
                self.tier,
                CompletionRequest(
                    model="",
                    cache_prefix=cache_prefix,
                    system=self._system_prompt(task),
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
                    max_tokens=min(16384, remaining_tokens),
                ),
                token_ceiling=remaining_tokens,
                task_id=str(task.id),
            )
            result = loop_outcome.result
            total_tokens += loop_outcome.tokens
            total_cost += loop_outcome.cost_usd
            last_model = result.model
            output = result.parse_as(ExecutionOutput)
            payload = output.model_dump(mode="json")
            if self._tool_loop.has_tools:
                payload["exploration"] = loop_outcome.exploration_summary()
            if not payload.get("files"):
                payload.pop("files", None)
            self._ensure_grounded_citations(task, context, payload)
            if self._workspace_runtime is not None:
                payload.update(
                    await self._workspace_runtime.apply(task, payload, strategy)
                )
                # Uma rodada de autocorreção sem operações devolvia um workspace
                # vazio e apagava os arquivos aplicados na rodada anterior — a
                # entrega (PR) publicaria nada. A evidência é cumulativa.
                cumulative_workspace = self._merge_workspace_evidence(
                    cumulative_workspace, payload.get("workspace")
                )
                payload["workspace"] = cumulative_workspace

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

    def _system_prompt(self, task: AgentTask) -> str:
        prompt = SYSTEM_PROMPT.format(capability=task.capability.value)
        if self._tool_loop.has_tools:
            prompt += TOOLS_GUIDANCE
            if self._tool_loop.has_tool("fetch_url"):
                prompt += WEB_TOOL_GUIDANCE
        return prompt

    def _build_user_content(
        self,
        task: AgentTask,
        context: dict[str, Any],
        *,
        previous_feedback: str,
        current_iteration_feedback: str,
    ) -> str:
        criteria = format_criteria(task.acceptance_criteria)
        user_content = (
            f"Tarefa: {task.title}\n\n"
            f"Descrição:\n{task.description}\n\n"
            f"Critérios de aceitação:\n{criteria}"
        )
        if previous_feedback:
            user_content += (
                f"\n\nFeedback operacional da tentativa anterior:\n{previous_feedback}"
            )
        architecture_guidance = context.get("architecture_policy_guidance")
        acceptance_guidance = context.get("acceptance_policy_guidance")
        if isinstance(acceptance_guidance, str) and acceptance_guidance:
            user_content += f"\n\nContrato de aceitação:\n{acceptance_guidance}"
        if isinstance(architecture_guidance, str) and architecture_guidance:
            user_content += f"\n\nRegras de dependências:\n{architecture_guidance}"
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
        if build_grounding_prefix(context):
            focus = format_evidence_focus(
                context.get("task_evidence_ids") or task.evidence_ids
            )
            if focus:
                user_content += f"\n\n{focus}"
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
    def _merge_workspace_evidence(
        previous: dict[str, Any] | None, current: Any
    ) -> dict[str, Any]:
        """Une o workspace de rodadas sucessivas de autocorreção.

        Arquivos aplicados/publicados/diffs acumulam por path (a rodada mais
        recente vence); remoções posteriores retiram o path dos publicados;
        o histórico de operações concatena. Checks (command_feedback),
        estratégia e demais chaves vêm sempre da rodada atual.
        """
        current_ws = dict(current) if isinstance(current, dict) else {}
        if not previous:
            return current_ws
        merged = {**previous, **current_ws}

        def str_list(source: dict[str, Any], key: str) -> list[str]:
            value = source.get(key)
            return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

        def path_items(source: dict[str, Any], key: str) -> list[dict[str, Any]]:
            value = source.get(key)
            return [
                v for v in value if isinstance(v, dict) and isinstance(v.get("path"), str)
            ] if isinstance(value, list) else []

        deleted_now = set(str_list(current_ws, "deleted_paths"))
        merged["applied_files"] = list(
            dict.fromkeys([*str_list(previous, "applied_files"), *str_list(current_ws, "applied_files")])
        )
        for key in ("published_files", "file_diffs"):
            by_path = {item["path"]: item for item in path_items(previous, key)}
            for path in deleted_now:
                by_path.pop(path, None)
            by_path.update({item["path"]: item for item in path_items(current_ws, key)})
            if by_path or key in previous or key in current_ws:
                merged[key] = list(by_path.values())
        republished = {item["path"] for item in path_items(current_ws, "published_files")}
        deleted = [
            p for p in dict.fromkeys([*str_list(previous, "deleted_paths"), *deleted_now])
            if p not in republished
        ]
        if deleted or "deleted_paths" in previous or "deleted_paths" in current_ws:
            merged["deleted_paths"] = deleted
        history = [
            *(previous.get("operation_history") or []),
            *(current_ws.get("operation_history") or []),
        ]
        if history:
            merged["operation_history"] = history
        return merged

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
