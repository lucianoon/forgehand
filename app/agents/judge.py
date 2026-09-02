"""Judge — avaliação combinada: LLM + sinais objetivos (regra 6).

Fase 1: a lista de validadores objetivos pode estar vazia; o judge declara
honestamente em validated_by o que conseguiu verificar. Fase 5 pluga pytest,
ruff e mypy via sandbox SEM tocar neste arquivo — só implementando o
protocolo ObjectiveValidator.

O veto é estrutural: approved = llm_approved AND todos os sinais objetivos.
O validator do EvaluationResult (models/task.py) rejeita qualquer instância
que viole isso — não é convenção, é contrato.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agents.grounding import (
    build_grounding_prefix,
    format_evidence_focus,
    grounding_required,
    normalize_citations,
    validate_citations,
)
from app.agents.tools import AgentTool, ToolLoop
from app.agents.validation import (
    ObjectiveValidationPipeline,
    ObjectiveValidator,
)
from app.models.task import AgentTask, EvaluationResult
from app.graph.nodes import JudgingOutcome, UsageReport
from app.providers.base import CompletionRequest, Message
from app.providers.registry import ModelTier, ProviderRouter

SYSTEM_PROMPT = """Você é o judge do Forgehand. Avalie o resultado de uma \
tarefa contra os critérios de aceitação.

Regras:
- avalie critério por critério, com score 0.0-1.0 e justificativa curta;
- um critério só pontua acima de 0.7 se está DEMONSTRADAMENTE atendido no \
resultado — ausência de evidência é reprovação, não benefício da dúvida;
- em failures, liste objetivamente o que está errado ou faltando;
- em required_changes, instruções ACIONÁVEIS para o executor corrigir — \
serão anexadas à próxima tentativa;
- approved=true somente se TODOS os critérios pontuam >= 0.7;
- avalie também: correção aparente, segurança, manutenibilidade e \
consistência interna entre os arquivos;
- quando houver grounding do repositório, trate ausência de citations válidas \
como falha estrutural."""

TOOLS_GUIDANCE = """

Ferramentas disponíveis: read_file, list_directory e search_repository. \
Quando o resultado declara arquivos aplicados, CONFIRA o conteúdo real no \
workspace em vez de confiar no resumo do executor. Poucas chamadas, então \
emita o veredito estruturado."""


class CriterionVerdict(BaseModel):
    criterion: str
    score: float = Field(ge=0, le=1)
    reasoning: str


class JudgeOutput(BaseModel):
    criteria: list[CriterionVerdict] = Field(min_length=1)
    failures: list[str] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)
    overall_score: float = Field(ge=0, le=1)
    approved: bool


class LLMJudge:
    """Implementa o protocolo Judge de app.graph.nodes."""

    def __init__(
        self,
        router: ProviderRouter,
        validators: list[ObjectiveValidator] | None = None,
        validation_pipeline: ObjectiveValidationPipeline | None = None,
        tier: ModelTier = ModelTier.STANDARD,
        tools: list[AgentTool] | None = None,
        max_tool_calls: int = 4,
    ):
        self._router = router
        self._tool_loop = ToolLoop(router, tools, max_tool_calls=max_tool_calls)
        self._validation_pipeline = validation_pipeline or ObjectiveValidationPipeline(
            validators or []
        )
        self._tier = tier

    @staticmethod
    def _grounding_failure(task: AgentTask, errors: list[str]) -> JudgingOutcome:
        required_changes = [
            "Inclua `citations` com evidence_ids reais do contexto do repositório.",
            "Restrinja o resultado ao escopo das evidências atribuídas à tarefa.",
        ]
        return JudgingOutcome(
            evaluation=EvaluationResult(
                task_id=task.id,
                approved=False,
                score=0.0,
                criteria_scores={
                    criterion: 0.0 for criterion in task.acceptance_criteria
                },
                failures=errors,
                required_changes=required_changes,
                validated_by=["grounding"],
            ),
            usage=UsageReport(),
        )

    @staticmethod
    def _is_citation_feedback(message: str) -> bool:
        lowered = message.lower()
        return "citation" in lowered or "citaç" in lowered or "evidence_id" in lowered

    @staticmethod
    def _criterion_is_citation_validity(criterion: str) -> bool:
        lowered = criterion.lower()
        citation_marker = (
            "citation" in lowered or "citaç" in lowered or "evidence_id" in lowered
        )
        validity_marker = any(
            marker in lowered for marker in ("válid", "valid", "exist", "grounding")
        )
        return citation_marker and validity_marker

    @staticmethod
    def _minimal_change_satisfied(task: AgentTask) -> bool:
        if not isinstance(task.result, dict):
            return False
        workspace = task.result.get("workspace")
        if not isinstance(workspace, dict):
            return False
        file_diffs = workspace.get("file_diffs")
        if not isinstance(file_diffs, list) or not file_diffs:
            return False
        changed_diffs = [
            item
            for item in file_diffs
            if isinstance(item, dict) and item.get("changed") is True
        ]
        if not changed_diffs:
            return False
        # Determinístico: só criações. `operation` vem do workspace runtime;
        # um replace/delete em arquivo existente nunca é "alteração mínima
        # restrita ao arquivo novo", independente do que o LLM achar.
        return all(
            item.get("change_type") == "created"
            and item.get("operation", "create") == "create"
            for item in changed_diffs
        )

    @staticmethod
    def _apply_failures(task: AgentTask) -> list[str]:
        """Operações que o runtime não conseguiu aplicar (search não encontrado,
        ambíguo...). Sinal objetivo: a mudança pretendida NÃO está no workspace."""
        if not isinstance(task.result, dict):
            return []
        workspace = task.result.get("workspace")
        if not isinstance(workspace, dict):
            return []
        errors = workspace.get("apply_errors")
        if not isinstance(errors, list):
            return []
        return [
            f"{item.get('operation')} {item.get('path')}: {item.get('error')}"
            for item in errors
            if isinstance(item, dict)
        ]

    @staticmethod
    def _criterion_is_minimal_change(criterion: str) -> bool:
        lowered = criterion.lower()
        return (
            "alteração mínima" in lowered
            or "restrita ao arquivo novo" in lowered
            or "não altere arquivos existentes" in lowered
        )

    @staticmethod
    def _is_minimal_change_feedback(message: str) -> bool:
        lowered = message.lower()
        return (
            "alteração mínima" in lowered
            or "arquivo novo" in lowered
            or "modificado após" in lowered
            or "sem modificações após sua criação" in lowered
            or "não atende ao critério de alteração mínima" in lowered
            or "não foi restrita" in lowered
        )

    async def evaluate(
        self, task: AgentTask, context: dict[str, Any]
    ) -> JudgingOutcome:
        criteria = "\n".join(f"- {c}" for c in task.acceptance_criteria)
        citations = normalize_citations(
            task.result.get("citations") if isinstance(task.result, dict) else None
        )
        citation_errors = validate_citations(
            context,
            citations,
            allowed_ids=task.evidence_ids or None,
        )
        if citation_errors:
            return self._grounding_failure(task, citation_errors)

        cache_prefix = build_grounding_prefix(context)
        prompt_content = (
            f"Tarefa: {task.title}\n\n"
            f"Descrição:\n{task.description}\n\n"
            f"Critérios de aceitação:\n{criteria}\n\n"
            f"Resultado do executor:\n{task.result}"
        )
        if cache_prefix:
            focus = format_evidence_focus(task.evidence_ids)
            if focus:
                prompt_content += f"\n\n{focus}"
        system_prompt = SYSTEM_PROMPT + (
            TOOLS_GUIDANCE if self._tool_loop.has_tools else ""
        )
        loop_outcome = await self._tool_loop.run(
            self._tier,
            CompletionRequest(
                model="",
                cache_prefix=cache_prefix,
                system=system_prompt,
                messages=[
                    Message(
                        role="user",
                        content=prompt_content,
                    )
                ],
                response_schema=JudgeOutput,
                max_tokens=8192,
            ),
        )
        verdict = loop_outcome.result.parse_as(JudgeOutput)

        # Sinais objetivos — veto estrutural sobre a opinião do LLM
        signals = await self._validation_pipeline.validate(task)
        by_name = {s.name: s for s in signals}
        apply_failures = self._apply_failures(task)
        objective_ok = (
            all(s.passed is not False for s in signals) and not apply_failures
        )

        criteria_scores = {c.criterion: c.score for c in verdict.criteria}
        minimal_change_ok = self._minimal_change_satisfied(task)
        for criterion in list(criteria_scores):
            if minimal_change_ok and self._criterion_is_minimal_change(criterion):
                criteria_scores[criterion] = 1.0

        failures = list(verdict.failures)
        for s in signals:
            if s.passed is False:
                failures.append(f"[{s.name}] {s.details}")
        failures.extend(f"[apply] {item}" for item in apply_failures)

        required_changes = list(verdict.required_changes)
        if minimal_change_ok:
            failures = [
                item for item in failures if not self._is_minimal_change_feedback(item)
            ]
            required_changes = [
                item
                for item in required_changes
                if not self._is_minimal_change_feedback(item)
            ]
        if grounding_required(context):
            # A existência e o escopo dos IDs já foram validados acima de forma
            # determinística. O LLM continua avaliando se a evidência sustenta a
            # análise, mas não pode contradizer a validade estrutural dos IDs.
            for criterion in list(criteria_scores):
                if self._criterion_is_citation_validity(criterion):
                    criteria_scores[criterion] = 1.0
            failures = [
                item for item in failures if not self._is_citation_feedback(item)
            ]
            required_changes = [
                item
                for item in required_changes
                if not self._is_citation_feedback(item)
            ]
        criteria_ok = bool(criteria_scores) and all(
            score >= 0.7 for score in criteria_scores.values()
        )
        normalized_approved = verdict.approved
        if criteria_ok and objective_ok and not failures and not required_changes:
            normalized_approved = True

        return JudgingOutcome(
            evaluation=EvaluationResult(
                task_id=task.id,
                approved=normalized_approved and objective_ok,
                score=verdict.overall_score
                if objective_ok
                else min(verdict.overall_score, 0.4),
                criteria_scores=criteria_scores,
                failures=failures,
                required_changes=required_changes,
                tests_passed=(
                    by_name["pytest"].passed if "pytest" in by_name else None
                ),
                lint_passed=by_name["ruff"].passed if "ruff" in by_name else None,
                type_check_passed=(
                    by_name["mypy"].passed if "mypy" in by_name else None
                ),
                validated_by=[
                    "llm",
                    *[s.name for s in signals if s.passed is not None],
                    *(["apply"] if apply_failures else []),
                ],
            ),
            usage=UsageReport(
                tokens=loop_outcome.tokens,
                cost_usd=loop_outcome.cost_usd,
            ),
        )
