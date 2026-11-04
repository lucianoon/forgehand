"""Camada Router do Super-Agent (Fast Path + Slow Path + Subscription Direct).

Responsabilidades (alinhadas ao design do dsh-superagent-prototype):
1. Receber eventos externos normalizados (Issues, PR comments, reviews, @mentions).
2. Aplicar Fast Path (código puro, custo zero):
   - Bloqueio anti-loop de bots
   - Verificação de allowlist de remetentes e repositórios
   - Subscription Direct: se a Thread já está assinada por um workflow ativo,
     entrega diretamente sem passar pelo julgamento do LLM do Router.
   - Modo proativo `off` descarta eventos sem `@forgehand` explícito.
3. Aplicar Slow Path (julgamento semântico em `ModelTier.FAST`):
   - Decide se o evento exige ação de engenharia, extrai resumo e critérios de aceitação.
   - Aplica a política proativa (`off` | `observe` | `auto`).
"""

from __future__ import annotations

import re
from typing import Iterable

from app.infrastructure.superagent_store import SuperAgentStore
from app.models.superagent import (
    IngressEvent,
    ProactiveMode,
    RouterDecisionStatus,
    RouterEvaluation,
    RouterTicket,
)
from app.providers.base import CompletionRequest, Message
from app.providers.registry import ModelTier, ProviderRouter

ROUTER_SYSTEM_PROMPT = """Você é o Router de triagem do Forgehand Super-Agent.
Sua única responsabilidade é decidir se um evento externo (Issue, comentário ou review de PR)
constitui uma demanda técnica acionável para um Worker de engenharia de software.

Regras:
- Você NÃO escreve código nem executa ferramentas; apenas classifica e estrutura o ticket.
- `should_act=true` apenas quando houver um pedido claro de implementação, correção de bug,
  refatoração, teste ou ajuste de código em PR.
- `should_act=false` para conversas casuais, agradecimentos ("obrigado", "+1", "LGTM"),
  spam ou perguntas vagas sem ação no repositório.
- Preencha `summary` com uma descrição objetiva da demanda (mínimo 10 caracteres).
- Preencha `acceptance_criteria` com 1 a 5 critérios verificáveis."""

_MENTION_PATTERN = re.compile(r"(?:^|\s)@forgehand\b", re.IGNORECASE)


def has_explicit_mention(text: str, bot_handle: str = "forgehand") -> bool:
    if not text:
        return False
    pattern = re.compile(rf"(?:^|\s)@{re.escape(bot_handle)}\b", re.IGNORECASE)
    return bool(pattern.search(text))


class SuperAgentRouter:
    """Roteador de dois níveis (Fast Path programático + Slow Path via ModelTier.FAST)."""

    def __init__(
        self,
        store: SuperAgentStore,
        provider_router: ProviderRouter | None = None,
        *,
        proactive_mode: ProactiveMode = ProactiveMode.OFF,
        allowed_senders: Iterable[str] | None = None,
        approved_repositories: Iterable[str] | None = None,
        bot_handle: str = "forgehand",
        confidence_threshold: float = 0.75,
        tier: ModelTier = ModelTier.FAST,
    ) -> None:
        self._store = store
        self._provider_router = provider_router
        self._proactive_mode = proactive_mode
        self._allowed_senders = (
            {s.strip().lower() for s in allowed_senders if s.strip()}
            if allowed_senders is not None
            else {"*"}
        )
        self._approved_repositories = (
            {r.strip().lower() for r in approved_repositories if r.strip()}
            if approved_repositories is not None
            else {"*"}
        )
        self._bot_handle = bot_handle.strip().lstrip("@").lower() or "forgehand"
        self._bot_handles = {
            self._bot_handle,
            f"{self._bot_handle}[bot]",
            "github-actions[bot]",
            "dependabot[bot]",
        }
        self._confidence_threshold = confidence_threshold
        self._tier = tier

    @property
    def proactive_mode(self) -> ProactiveMode:
        return self._proactive_mode

    async def route(self, event: IngressEvent) -> RouterTicket:
        existing = self._store.get_ticket_by_event_id(event.event_id)
        if existing is not None:
            return existing

        combined_text = f"{event.title}\n{event.body}".strip()
        explicit = event.explicit_mention or has_explicit_mention(
            combined_text, self._bot_handle
        )

        # ------------------------------------------------------------ Fast Path
        sender_lower = event.sender.strip().lower()
        if (
            event.sender_is_bot
            or sender_lower in self._bot_handles
            or sender_lower.endswith("[bot]")
        ):
            return self._record_fast_rejection(
                event, combined_text, "sender_is_bot"
            )

        if (
            "*" not in self._allowed_senders
            and sender_lower not in self._allowed_senders
        ):
            return self._record_fast_rejection(
                event, combined_text, "sender_not_in_allowlist"
            )

        repo_lower = event.repository.strip().lower()
        if (
            "*" not in self._approved_repositories
            and repo_lower not in self._approved_repositories
        ):
            return self._record_fast_rejection(
                event, combined_text, "repository_not_approved"
            )

        # Subscription Direct: Threads já assinadas contornam o Router
        subscription = self._store.get_active_subscription(
            event.repository, event.thread_kind, event.thread_number
        )
        if subscription is not None:
            self._store.record_direct_delivery(
                event.repository,
                event.thread_kind,
                event.thread_number,
                event.event_id,
            )
            summary = (
                combined_text[:300]
                if len(combined_text) >= 10
                else f"Atualização na thread #{event.thread_number}: {combined_text}"
            )
            ticket = RouterTicket(
                event_id=event.event_id,
                repository=event.repository,
                scm_host=event.scm_host,
                base_ref=event.base_ref,
                thread_kind=event.thread_kind,
                thread_number=event.thread_number,
                sender=event.sender,
                title=event.title,
                summary=summary,
                acceptance_criteria=[
                    "Atender ao feedback mais recente da thread mantendo os critérios anteriores."
                ],
                proactive_mode=self._proactive_mode,
                status=RouterDecisionStatus.DIRECT_SUBSCRIPTION,
                confidence=1.0,
                reasoning="subscription_direct_bypass",
                workflow_id=subscription.workflow_id,
                source_url=event.html_url,
            )
            return self._store.save_ticket(ticket)

        if not explicit and self._proactive_mode == ProactiveMode.OFF:
            return self._record_fast_rejection(
                event, combined_text, "proactive_mode_off_without_mention"
            )

        cleaned_body = re.sub(
            rf"@{re.escape(self._bot_handle)}\b", "", combined_text, flags=re.IGNORECASE
        ).strip()
        if len(cleaned_body) < 10:
            return self._record_fast_rejection(
                event, combined_text, "insufficient_actionable_content"
            )

        # ------------------------------------------------------------ Slow Path
        evaluation = await self._evaluate_slow_path(event, cleaned_body, explicit)
        if (
            not evaluation.should_act
            or evaluation.confidence < self._confidence_threshold
        ):
            ticket = RouterTicket(
                event_id=event.event_id,
                repository=event.repository,
                scm_host=event.scm_host,
                base_ref=event.base_ref,
                thread_kind=event.thread_kind,
                thread_number=event.thread_number,
                sender=event.sender,
                title=event.title,
                summary=evaluation.summary,
                acceptance_criteria=evaluation.acceptance_criteria,
                proactive_mode=self._proactive_mode,
                status=RouterDecisionStatus.REJECTED_SLOW_PATH,
                confidence=evaluation.confidence,
                reasoning=evaluation.reasoning,
                source_url=event.html_url,
            )
            return self._store.save_ticket(ticket)

        target_status = (
            RouterDecisionStatus.DISPATCHED
            if (explicit or self._proactive_mode == ProactiveMode.AUTO)
            else RouterDecisionStatus.OBSERVED
        )
        ticket = RouterTicket(
            event_id=event.event_id,
            repository=event.repository,
            scm_host=event.scm_host,
            base_ref=event.base_ref,
            thread_kind=event.thread_kind,
            thread_number=event.thread_number,
            sender=event.sender,
            title=event.title,
            summary=evaluation.summary,
            acceptance_criteria=evaluation.acceptance_criteria,
            proactive_mode=self._proactive_mode,
            status=target_status,
            confidence=evaluation.confidence,
            reasoning=evaluation.reasoning,
            source_url=event.html_url,
        )
        return self._store.save_ticket(ticket)

    def _record_fast_rejection(
        self, event: IngressEvent, combined_text: str, reason: str
    ) -> RouterTicket:
        summary = (combined_text[:200] or event.kind.value).strip()
        ticket = RouterTicket(
            event_id=event.event_id,
            repository=event.repository,
            scm_host=event.scm_host,
            base_ref=event.base_ref,
            thread_kind=event.thread_kind,
            thread_number=event.thread_number,
            sender=event.sender,
            title=event.title,
            summary=summary or "evento_descartado",
            acceptance_criteria=[],
            proactive_mode=self._proactive_mode,
            status=RouterDecisionStatus.REJECTED_FAST_PATH,
            confidence=0.0,
            reasoning=reason,
            source_url=event.html_url,
        )
        return self._store.save_ticket(ticket)

    async def _evaluate_slow_path(
        self, event: IngressEvent, cleaned_body: str, explicit: bool
    ) -> RouterEvaluation:
        if self._provider_router is None:
            # Fallback determinístico quando nenhum LLM está acoplado ao Router
            summary = (
                f"{event.title}: {cleaned_body}".strip(": ")
                if event.title
                else cleaned_body
            )[:400]
            if len(summary) < 10:
                summary = f"Resolver demanda da thread #{event.thread_number}: {summary}"
            return RouterEvaluation(
                should_act=True,
                confidence=0.95 if explicit else 0.85,
                summary=summary,
                acceptance_criteria=[
                    "Implementar a alteração solicitada e garantir que os testes passem."
                ],
                reasoning=(
                    "Solicitação explícita via @mention"
                    if explicit
                    else "Candidato proativo identificado por heurística local"
                ),
            )

        user_prompt = (
            f"Repositório: {event.repository}\n"
            f"Tipo de evento: {event.kind.value}\n"
            f"Thread: {event.thread_kind} #{event.thread_number}\n"
            f"Remetente: {event.sender}\n"
            f"Menção explícita (@{self._bot_handle}): {explicit}\n"
            f"Modo proativo configurado: {self._proactive_mode.value}\n\n"
            f"Título:\n{event.title or '(sem título)'}\n\n"
            f"Conteúdo:\n{event.body[:4000]}"
        )
        result = await self._provider_router.complete(
            self._tier,
            CompletionRequest(
                model="",
                system=ROUTER_SYSTEM_PROMPT,
                messages=[Message(role="user", content=user_prompt)],
                response_schema=RouterEvaluation,
                max_tokens=1024,
            ),
        )
        return result.parse_as(RouterEvaluation)

