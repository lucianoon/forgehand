"""Registry de providers e roteamento por tier.

Enforcement da regra 9: modelos caros entram POR ESCALONAMENTO, nunca por
default. O agente pede um tier ("preciso de raciocínio forte") ou deixa o
advisor escalar — quem resolve tier→(provider, modelo) é o registry, a
partir de configuração.

Interface única para os agentes: `router.complete(tier, request_sem_model)`.
"""

from __future__ import annotations

import time
from enum import IntEnum
from typing import Any

from pydantic import BaseModel

from app.providers.base import (
    CompletionRequest,
    CompletionResult,
    LLMProvider,
    ProviderError,
)


class ModelTier(IntEnum):
    """IntEnum de propósito: escalonamento é tier + 1."""

    FAST = 1  # classificação, extração, tarefas mecânicas
    STANDARD = 2  # execução de código, documentação — o default
    STRONG = 3  # advisor, arquitetura, replan após falhas


class TierBinding(BaseModel):
    provider_name: str
    model: str

    model_config = {"frozen": True}


class ProviderRouter:
    def __init__(
        self,
        providers: dict[str, LLMProvider],
        bindings: dict[ModelTier, TierBinding],
        default_tier: ModelTier = ModelTier.STANDARD,
        tracer: Any | None = None,
    ):
        missing = {b.provider_name for b in bindings.values()} - set(providers)
        if missing:
            raise ValueError(f"Bindings referenciam providers ausentes: {missing}")
        if ModelTier.STANDARD not in bindings:
            raise ValueError("Binding para STANDARD é obrigatório.")

        self._providers = providers
        self._bindings = bindings
        self._default_tier = default_tier
        # Instrumentação na porta única (regra 1): todo consumo de LLM do
        # sistema passa por aqui, então todo consumo é rastreado aqui.
        self._tracer = tracer

    def resolve(self, tier: ModelTier) -> tuple[LLMProvider, str]:
        """Resolve tier → (provider, modelo). Tier sem binding cai para o
        binding mais forte disponível ABAIXO dele — nunca escala sozinho."""
        candidate = int(tier)
        while candidate >= int(ModelTier.FAST):
            binding = self._bindings.get(ModelTier(candidate))
            if binding is not None:
                return self._providers[binding.provider_name], binding.model
            candidate -= 1
        # STANDARD é garantido no __init__
        binding = self._bindings[ModelTier.STANDARD]
        return self._providers[binding.provider_name], binding.model

    def escalate(self, current: ModelTier) -> ModelTier:
        """Próximo tier acima. Chamado APENAS pelo fluxo do advisor —
        nenhum executor escala a si mesmo."""
        return ModelTier(min(current + 1, ModelTier.STRONG))

    async def complete(
        self,
        tier: ModelTier | None,
        request: CompletionRequest,
    ) -> CompletionResult:
        """Ponto único de chamada dos agentes. `request.model` é ignorado
        e substituído pela resolução do tier — agente não escolhe modelo."""
        resolved_tier = tier or self._default_tier
        provider, model = self.resolve(resolved_tier)
        bound = request.model_copy(update={"model": model})
        if self._tracer is None:
            return await provider.complete(bound)

        started = time.monotonic()
        try:
            result = await provider.complete(bound)
        except ProviderError as exc:
            self._tracer.record_generation(
                provider=provider.name,
                model=model,
                tier=int(resolved_tier),
                latency_ms=(time.monotonic() - started) * 1000,
                error=type(exc).__name__,
            )
            raise
        self._tracer.record_generation(
            provider=result.provider,
            model=result.model,
            tier=int(resolved_tier),
            latency_ms=result.latency_ms,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cost_usd=result.cost_usd,
        )
        return result
