"""Advisor — consultor de tier STRONG para tarefas em dificuldade (Fase 3).

Consultado pelo replan quando os sinais OBJETIVOS do AdvisorTrigger disparam
(models/task.py): rejeições do judge, diff repetido, budget esgotado, tarefa
crítica. Nunca por opinião do próprio executor.

Produz diagnóstico + orientação acionável que entram no prompt da próxima
tentativa, e é o ÚNICO fluxo autorizado a recomendar escalonamento de tier
(regra 8: providers/registry.py escalate() — executor nunca escala a si mesmo).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.graph.nodes import AdvisingOutcome, UsageReport
from app.models.task import AdvisorTrigger, EvaluationResult, TaskAttempt
from app.providers.base import CompletionRequest, Message
from app.providers.registry import ModelTier, ProviderRouter

SYSTEM_PROMPT = """Você é o advisor do Forgehand — um consultor sênior \
chamado quando uma tarefa está falhando repetidamente.

Analise o histórico de tentativas, as reprovações do judge e o feedback
operacional. Regras:
- em diagnosis, explique a causa raiz provável das falhas em 2-4 frases;
- em guidance, instruções ACIONÁVEIS e específicas para a próxima tentativa —
  o executor vai recebê-las literalmente no prompt;
- escalate_tier=true SOMENTE quando o problema aparenta exigir mais capacidade
  de raciocínio do modelo; instrução ambígua ou falta de contexto se resolve
  com guidance, não com modelo mais caro;
- se as tentativas anteriores repetem a mesma solução, proponha uma abordagem
  DIFERENTE, não um refinamento da mesma."""

_MAX_FEEDBACK_CHARS = 2000


class AdvisorOutput(BaseModel):
    diagnosis: str
    guidance: list[str] = Field(min_length=1)
    escalate_tier: bool = False


class LLMAdvisor:
    """Implementa o protocolo Advisor de app.graph.nodes."""

    def __init__(self, router: ProviderRouter, tier: ModelTier = ModelTier.STRONG):
        self._router = router
        self._tier = tier

    async def advise(
        self,
        trigger: AdvisorTrigger,
        evaluations: list[EvaluationResult],
        context: dict[str, Any],
    ) -> AdvisingOutcome:
        result = await self._router.complete(
            self._tier,
            CompletionRequest(
                model="",
                system=SYSTEM_PROMPT,
                messages=[
                    Message(
                        role="user",
                        content=self._build_user_content(trigger, evaluations),
                    )
                ],
                response_schema=AdvisorOutput,
                max_tokens=4096,
            ),
        )
        output = result.parse_as(AdvisorOutput)
        return AdvisingOutcome(
            diagnosis=output.diagnosis,
            guidance=output.guidance,
            escalate_tier=output.escalate_tier,
            usage=UsageReport(
                tokens=result.usage.total_tokens,
                cost_usd=result.cost_usd,
            ),
        )

    @staticmethod
    def _attempt_line(attempt: TaskAttempt) -> str:
        line = (
            f"Tentativa {attempt.attempt_number} "
            f"({attempt.agent_name}, {attempt.model}): "
            f"{attempt.failure_reason or 'sem falha técnica registrada'}"
        )
        summary = attempt.operational_summary or {}
        feedback = summary.get("validation_feedback_text")
        if isinstance(feedback, str) and feedback:
            line += f"\n{feedback[:_MAX_FEEDBACK_CHARS]}"
        return line

    def _build_user_content(
        self, trigger: AdvisorTrigger, evaluations: list[EvaluationResult]
    ) -> str:
        task = trigger.task
        criteria = "\n".join(f"- {c}" for c in task.acceptance_criteria)
        lines = [
            f"Tarefa: {task.title}",
            "",
            f"Descrição:\n{task.description}",
            "",
            f"Critérios de aceitação:\n{criteria}",
            "",
            "Sinais objetivos: "
            f"tentativas={task.attempt_count}, "
            f"reprovações do judge={trigger.judge_rejections}, "
            f"diff repetido={trigger.repeated_diff_detected}, "
            f"budget esgotado={task.budget.exhausted}, "
            f"tarefa crítica={task.is_critical}",
        ]
        history = [self._attempt_line(a) for a in task.attempts[-3:]]
        if history:
            lines += ["", "Histórico de tentativas:", *history]
        for ev in [e for e in evaluations if e.task_id == task.id][-2:]:
            if ev.failures:
                lines += [
                    "",
                    "Falhas apontadas pelo judge:",
                    *[f"- {f}" for f in ev.failures],
                ]
            if ev.required_changes:
                lines += [
                    "Correções já exigidas pelo judge:",
                    *[f"- {c}" for c in ev.required_changes],
                ]
        return "\n".join(lines)
