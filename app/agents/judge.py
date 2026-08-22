"""Judge — avaliação combinada: LLM + sinais objetivos (regra 6).

Fase 1: a lista de validadores objetivos pode estar vazia; o judge declara
honestamente em validated_by o que conseguiu verificar. Fase 5 pluga pytest,
ruff e mypy via sandbox SEM tocar neste arquivo — só implementando o
protocolo ObjectiveValidator.

O veto é estrutural: approved = llm_approved AND todos os sinais objetivos.
O validator do EvaluationResult (models/task.py) rejeita qualquer instância
que viole isso — não é convenção, é contrato.

Além dos validadores externos, o runtime já verificou fatos sobre a própria
tentativa (citations válidas, diff aplicado). Esses fatos entram no prompt com
id estável e o LLM marca quais critérios e observações os invocam — ver
app/agents/deterministic_checks.py. A reconciliação é feita por id, nunca
interpretando o texto do LLM.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.agents.deterministic_checks import (
    DeterministicCheck,
    active_checks,
    format_checks_block,
)
from app.agents.grounding import (
    format_repository_grounding,
    normalize_citations,
    validate_citations,
)
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
como falha estrutural;
- quando o prompt trouxer fatos verificados, marque `deterministic_check` com \
o id do fato em todo critério, falha ou correção que se refira a ele."""


class JudgeFinding(BaseModel):
    """Observação do judge, opcionalmente ancorada num fato verificado."""

    message: str
    deterministic_check: str | None = Field(
        default=None,
        description=(
            "id do fato verificado que esta observação invoca, exatamente como "
            "listado no bloco de fatos verificados. Nulo quando não se aplica."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_plain_string(cls, value: Any) -> Any:
        """Degrada com segurança quando o endpoint não honra o JSON Schema.

        Provider sem structured output estrito (modelos locais) devolve string
        pura. Sem âncora, a observação é preservada — o lado seguro."""
        if isinstance(value, str):
            return {"message": value}
        return value


class CriterionVerdict(BaseModel):
    criterion: str
    score: float = Field(ge=0, le=1)
    reasoning: str
    deterministic_check: str | None = Field(
        default=None,
        description=(
            "id do fato verificado que este critério reformula, exatamente como "
            "listado no bloco de fatos verificados. Nulo quando não se aplica."
        ),
    )


class JudgeOutput(BaseModel):
    criteria: list[CriterionVerdict] = Field(min_length=1)
    failures: list[JudgeFinding] = Field(default_factory=list)
    required_changes: list[JudgeFinding] = Field(default_factory=list)
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
    ):
        self._router = router
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
    def _reconcile(
        findings: list[JudgeFinding],
        checks_by_id: dict[str, DeterministicCheck],
    ) -> list[str]:
        """Descarta observações ancoradas num fato que se confirma.

        Uma observação sem âncora — ou ancorada num fato que NÃO se confirma —
        é preservada: o sistema só sobrepõe o LLM onde já verificou o contrário.
        """
        kept: list[str] = []
        for finding in findings:
            check = checks_by_id.get(finding.deterministic_check or "")
            if check is not None and check.holds:
                continue
            kept.append(finding.message)
        return kept

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

        # Citations já validadas acima; qualquer outro fato aplicável entra junto.
        checks = active_checks(task, context, citations_are_valid=True)
        checks_by_id = {check.id: check for check in checks}

        grounding_block = format_repository_grounding(
            context,
            evidence_ids=task.evidence_ids,
            max_items=8,
        )
        prompt_content = (
            f"Tarefa: {task.title}\n\n"
            f"Descrição:\n{task.description}\n\n"
            f"Critérios de aceitação:\n{criteria}\n\n"
            f"Resultado do executor:\n{task.result}"
        )
        if grounding_block:
            prompt_content += f"\n\n{grounding_block}"
        checks_block = format_checks_block(checks)
        if checks_block:
            prompt_content += f"\n\n{checks_block}"
        result = await self._router.complete(
            self._tier,
            CompletionRequest(
                model="",
                system=SYSTEM_PROMPT,
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
        verdict = result.parse_as(JudgeOutput)

        # Sinais objetivos — veto estrutural sobre a opinião do LLM
        signals = await self._validation_pipeline.validate(task)
        by_name = {s.name: s for s in signals}
        objective_ok = all(s.passed is not False for s in signals)

        # Fato verificado decide o critério que o invoca, nos dois sentidos.
        criteria_scores: dict[str, float] = {}
        for verdict_item in verdict.criteria:
            check = checks_by_id.get(verdict_item.deterministic_check or "")
            criteria_scores[verdict_item.criterion] = (
                (1.0 if check.holds else 0.0)
                if check is not None
                else verdict_item.score
            )

        failures = self._reconcile(verdict.failures, checks_by_id)
        required_changes = self._reconcile(verdict.required_changes, checks_by_id)
        for s in signals:
            if s.passed is False:
                failures.append(f"[{s.name}] {s.details}")

        criteria_ok = bool(criteria_scores) and all(
            score >= 0.7 for score in criteria_scores.values()
        )
        # Contrato do prompt: aprovar exige TODOS os critérios >= 0.7. A regra
        # vale nos dois sentidos, inclusive quando o score veio de um fato
        # verificado — sem isso um fato que não se confirma zeraria o critério
        # e ainda assim deixaria passar a aprovação do LLM. O segundo ramo
        # normaliza a saída inconsistente (approved=false sem apontar um único
        # problema, com todos os critérios passando).
        normalized_approved = criteria_ok and (
            verdict.approved or (objective_ok and not failures and not required_changes)
        )

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
                    *[check.id for check in checks],
                    *[s.name for s in signals if s.passed is not None],
                ],
            ),
            usage=UsageReport(
                tokens=result.usage.total_tokens,
                cost_usd=result.cost_usd,
            ),
        )
