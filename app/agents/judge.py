"""Judge — critérios objetivos por código, subjetivos pelo LLM (regra 6).

Ordem de decisão:
1. grounding estrutural: citations inexistentes/fora de escopo reprovam antes
   de qualquer chamada de LLM;
2. sinais objetivos (pytest/ruff/mypy) e falhas de aplicação de operações;
3. critérios tipados objetivos (app.agents.criteria): 1.0 ou 0.0, sem LLM;
4. o LLM vê SÓ os critérios subjetivos (mais os objetivos que não puderam
   ser verificados, com essa nota) e devolve score por critério.

O veto é estrutural: approved = todos os critérios >= 0.7 E nenhum sinal
objetivo falhando. O validator do EvaluationResult (models/task.py) rejeita
qualquer instância que viole isso — não é convenção, é contrato. Falhas
textuais do LLM só entram quando ele reprovou um critério que era dele.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agents.criteria import evaluate_objective_criteria
from app.agents.grounding import (
    build_grounding_prefix,
    format_evidence_focus,
    grounding_required,
    normalize_citations,
    validate_citations,
)
from app.agents.tools import AgentTool, ToolLoop
from app.agents.hooks import ToolHookDispatcher
from app.agents.validation import (
    ObjectiveValidationPipeline,
    ObjectiveValidator,
)
from app.graph.nodes import JudgingOutcome, UsageReport
from app.models.task import AcceptanceCriterion, AgentTask, EvaluationResult
from app.providers.base import CompletionRequest, Message
from app.providers.registry import ModelTier, ProviderRouter

PASS_THRESHOLD = 0.7

SYSTEM_PROMPT = """Você é o judge do Forgehand. Avalie o resultado de uma \
tarefa contra os critérios de aceitação listados — SOMENTE esses.

Regras:
- os critérios vêm numerados; devolva um veredito por critério com o mesmo \
`index`, score 0.0-1.0 e justificativa curta;
- um critério só pontua acima de 0.7 se está DEMONSTRADAMENTE atendido no \
resultado — ausência de evidência é reprovação, não benefício da dúvida;
- em failures, liste objetivamente o que está errado ou faltando NOS \
CRITÉRIOS AVALIADOS; em required_changes, instruções ACIONÁVEIS para o \
executor corrigir — serão anexadas à próxima tentativa;
- approved=true somente se TODOS os critérios avaliados pontuam >= 0.7;
- avalie também: correção aparente, segurança, manutenibilidade e \
consistência interna entre os arquivos;
- critérios objetivos (testes, lint, tipos, arquivos criados/alterados, \
conteúdo, validade de citations) já foram verificados por código e NÃO estão \
na sua lista; não os julgue nem os mencione em failures. Quando um critério \
vier marcado como "não verificável automaticamente", avalie-o pelo resultado."""

TOOLS_GUIDANCE = """

Ferramentas disponíveis: read_file, list_directory e search_repository. \
Quando o resultado declara arquivos aplicados, CONFIRA o conteúdo real no \
workspace em vez de confiar no resumo do executor. Poucas chamadas, então \
emita o veredito estruturado."""


class CriterionVerdict(BaseModel):
    index: int | None = Field(
        default=None, description="Número do critério na lista enviada (1 = primeiro)."
    )
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
        independence: str = "bindings",
        critical_quorum: int = 1,
        hooks: ToolHookDispatcher | None = None,
    ):
        self._router = router
        self._tool_loop = ToolLoop(
            router,
            tools,
            max_tool_calls=max_tool_calls,
            hooks=hooks,
            agent_name="judge",
        )
        # "off": não registra; "bindings": usa os bindings do papel "judge" e
        # registra se o modelo coincidiu com o do executor; "escalate": pede ao
        # router outro modelo (tier acima, depois abaixo) para garantir a
        # independência. Tarefas críticas recebem `critical_quorum` vereditos.
        self._independence = independence
        self._critical_quorum = max(1, critical_quorum)
        self._validation_pipeline = validation_pipeline or ObjectiveValidationPipeline(
            validators or []
        )
        self._tier = tier

    # ------------------------------------------------------------------
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
                criteria_scores={c.text: 0.0 for c in task.acceptance_criteria},
                failures=errors,
                required_changes=required_changes,
                validated_by=["grounding"],
            ),
            usage=UsageReport(),
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

    # ------------------------------------------------------------------
    async def evaluate(
        self, task: AgentTask, context: dict[str, Any]
    ) -> JudgingOutcome:
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

        # Sinais objetivos — veto estrutural sobre a opinião do LLM
        signals = await self._validation_pipeline.validate(task)
        by_name = {s.name: s for s in signals}
        apply_failures = self._apply_failures(task)
        objective_ok = (
            all(s.passed is not False for s in signals) and not apply_failures
        )

        # Critérios objetivos: decididos por código
        criteria_scores: dict[str, float] = {}
        failures: list[str] = []
        required_changes: list[str] = []
        for_llm: list[tuple[AcceptanceCriterion, str | None]] = []
        objective_verdicts = evaluate_objective_criteria(task, context, by_name)
        for verdict in objective_verdicts:
            if verdict.passed is None:
                for_llm.append((verdict.criterion, verdict.detail))
                continue
            criteria_scores[verdict.criterion.text] = verdict.score
            if not verdict.passed:
                failures.append(f"[{verdict.criterion.kind.value}] {verdict.detail}")
                if verdict.required_change:
                    required_changes.append(verdict.required_change)
        for criterion in task.acceptance_criteria:
            if not criterion.kind.is_objective:
                for_llm.append((criterion, None))

        # Critérios subjetivos (e objetivos não verificáveis): LLM
        usage = UsageReport()
        llm_overall: float | None = None
        judge_models: list[str] = []
        independent: bool | None = None
        if for_llm:
            executor_model = self._executor_model(task)
            rounds = self._critical_quorum if task.is_critical else 1
            verdicts: list[tuple[JudgeOutput, str]] = []
            total_tokens = 0
            total_cost = 0.0
            for _ in range(rounds):
                request = self._build_request(task, context, for_llm).model_copy(
                    update={
                        "role": "judge",
                        "avoid_models": self._avoid_models(
                            executor_model, [model for _, model in verdicts]
                        ),
                    }
                )
                loop_outcome = await self._tool_loop.run(
                    self._tier, request, task_id=str(task.id)
                )
                total_tokens += loop_outcome.tokens
                total_cost += loop_outcome.cost_usd
                verdicts.append(
                    (
                        loop_outcome.result.parse_as(JudgeOutput),
                        loop_outcome.result.model,
                    )
                )
            usage = UsageReport(tokens=total_tokens, cost_usd=total_cost)
            judge_models = [model for _, model in verdicts]
            if self._independence != "off" and executor_model is not None:
                independent = executor_model not in judge_models
            llm_overall = min(verdict.overall_score for verdict, _ in verdicts)

            # Quórum: cada critério recebe a MENOR nota entre os juízes —
            # aprovação exige unanimidade, reprovação de um basta.
            per_round = [self._match_scores(for_llm, v) for v, _ in verdicts]
            llm_scores = {
                text: min(scores[text] for scores in per_round) for text in per_round[0]
            }
            criteria_scores.update(llm_scores)
            rejecting = [
                (verdict, model)
                for (verdict, model), scores in zip(verdicts, per_round, strict=True)
                if any(score < PASS_THRESHOLD for score in scores.values())
            ]
            if rejecting:
                tag_models = len(verdicts) > 1
                for llm_verdict, model in rejecting:
                    tag = f"[judge {model}] " if tag_models else ""
                    failures.extend(tag + item for item in llm_verdict.failures)
                    required_changes.extend(llm_verdict.required_changes)
                if tag_models and len(rejecting) < len(verdicts):
                    failures.append(
                        f"[quorum] juízes divergiram: {len(rejecting)} de "
                        f"{len(verdicts)} reprovaram; a aprovação exige unanimidade."
                    )

        for s in signals:
            if s.passed is False:
                failures.append(f"[{s.name}] {s.details}")
        failures.extend(f"[apply] {item}" for item in apply_failures)

        criteria_ok = bool(criteria_scores) and all(
            score >= PASS_THRESHOLD for score in criteria_scores.values()
        )
        approved = criteria_ok and objective_ok
        if criteria_scores:
            score = sum(criteria_scores.values()) / len(criteria_scores)
        else:
            score = llm_overall if llm_overall is not None else 0.0
        if not objective_ok:
            score = min(score, 0.4)

        validated_by: list[str] = []
        if for_llm:
            validated_by.append("llm")
        if any(v.passed is not None for v in objective_verdicts):
            validated_by.append("criteria")
        validated_by.extend(s.name for s in signals if s.passed is not None)
        if apply_failures:
            validated_by.append("apply")

        return JudgingOutcome(
            evaluation=EvaluationResult(
                task_id=task.id,
                approved=approved,
                score=score,
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
                validated_by=validated_by,
                judge_models=judge_models,
                independent_judge=independent,
            ),
            usage=usage,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _executor_model(task: AgentTask) -> str | None:
        """Modelo que produziu o resultado sob julgamento (última tentativa)."""
        if not task.attempts:
            return None
        model = task.attempts[-1].model
        return model if model and model != "unknown" else None

    def _avoid_models(
        self, executor_model: str | None, judges_used: list[str]
    ) -> list[str]:
        """Só em `escalate` o judge pede ao router que troque de modelo: evita
        o executor e, no quórum, os juízes já usados. Em `bindings` a
        independência vem da configuração e é apenas registrada."""
        if self._independence != "escalate":
            return []
        avoid = list(judges_used)
        if executor_model is not None:
            avoid.append(executor_model)
        return avoid

    def _build_request(
        self,
        task: AgentTask,
        context: dict[str, Any],
        for_llm: list[tuple[AcceptanceCriterion, str | None]],
    ) -> CompletionRequest:
        lines = []
        for index, (criterion, note) in enumerate(for_llm, start=1):
            line = f"{index}. {criterion.text}"
            if note:
                line += f" (não verificável automaticamente: {note})"
            lines.append(line)
        prompt_content = (
            f"Tarefa: {task.title}\n\n"
            f"Descrição:\n{task.description}\n\n"
            f"Critérios de aceitação a avaliar:\n" + "\n".join(lines) + "\n\n"
            f"Resultado do executor:\n{task.result}"
        )
        cache_prefix = build_grounding_prefix(context)
        if cache_prefix:
            focus = format_evidence_focus(task.evidence_ids)
            if focus:
                prompt_content += f"\n\n{focus}"
            if grounding_required(context):
                prompt_content += (
                    "\n\nA existência e o escopo das citations já foram validados "
                    "estruturalmente; não reavalie isso."
                )
        system_prompt = SYSTEM_PROMPT + (
            TOOLS_GUIDANCE if self._tool_loop.has_tools else ""
        )
        return CompletionRequest(
            model="",
            cache_prefix=cache_prefix,
            system=system_prompt,
            messages=[Message(role="user", content=prompt_content)],
            response_schema=JudgeOutput,
            max_tokens=8192,
        )

    @staticmethod
    def _match_scores(
        for_llm: list[tuple[AcceptanceCriterion, str | None]],
        verdict: JudgeOutput,
    ) -> dict[str, float]:
        """Casa vereditos com os critérios enviados: por índice, depois por
        texto (exato, depois normalizado). Critério sem veredito recebe o
        overall_score do LLM se ele aprovou, 0.0 se reprovou — nunca fica
        sem nota, porque nota ausente viraria aprovação por omissão."""
        by_index = {v.index: v for v in verdict.criteria if v.index is not None}
        by_text = {v.criterion: v for v in verdict.criteria}
        by_norm = {v.criterion.strip().lower(): v for v in verdict.criteria}
        scores: dict[str, float] = {}
        for index, (criterion, _) in enumerate(for_llm, start=1):
            match = (
                by_index.get(index)
                or by_text.get(criterion.text)
                or by_norm.get(criterion.text.strip().lower())
            )
            if match is not None:
                scores[criterion.text] = match.score
            else:
                scores[criterion.text] = (
                    verdict.overall_score if verdict.approved else 0.0
                )
        return scores
