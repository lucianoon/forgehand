"""Camada de providers — a ÚNICA porta de saída para LLMs (regra 1).

Nenhum agente importa SDK de fornecedor. Agentes falam CompletionRequest e
recebem CompletionResult; o resto é problema desta camada:

- saída estruturada validada por Pydantic (regra 2);
- timeout por chamada (regra 4);
- retry com backoff exponencial + jitter para erros transitórios;
- circuit breaker por provider — falha rápida quando o fornecedor degrada,
  em vez de enfileirar timeouts (padrão do plano FDE);
- custo calculado por tabela de preços INJETADA via settings, nunca
  hardcoded — preço muda sem redeploy;
- usage sempre reportado para o budget do workflow (regra 9 com enforcement).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel, Field, ValidationError

from app.infrastructure.llm_budget import active_call_budget

logger = logging.getLogger("forgehand.providers")

T = TypeVar("T", bound=BaseModel)


# --------------------------------------------------------------------------
# Erros — a distinção retryable/não-retryable dirige o retry e o breaker
# --------------------------------------------------------------------------


class ProviderError(Exception):
    """Base. Carrega o provider para rastreabilidade."""

    def __init__(self, message: str, provider: str, status_code: int | None = None):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class RetryableProviderError(ProviderError):
    """429, 5xx, timeout de rede, overloaded — vale tentar de novo."""


class NonRetryableProviderError(ProviderError):
    """400, 401, 403, 404, schema inválido — retry só queima budget."""


class CircuitOpenError(ProviderError):
    """Breaker aberto — falha imediata sem tocar o fornecedor."""


class StructuredOutputError(NonRetryableProviderError):
    """Modelo respondeu, mas a saída não valida contra o schema pedido."""


# --------------------------------------------------------------------------
# Contratos
# --------------------------------------------------------------------------


class ToolSpec(BaseModel):
    """Ferramenta oferecida ao modelo. `input_schema` é JSON Schema de objeto."""

    name: str
    description: str
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


class ToolCall(BaseModel):
    """Pedido do modelo para executar uma ferramenta (bloco tool_use)."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Resultado devolvido ao modelo (bloco tool_result)."""

    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


class Message(BaseModel):
    role: str  # "user" | "assistant"
    content: str = ""
    # assistant: chamadas que o modelo fez nesta mensagem
    tool_calls: list[ToolCall] = Field(default_factory=list)
    # user: resultados das chamadas da mensagem assistant anterior
    tool_results: list[ToolResult] = Field(default_factory=list)


class CompletionRequest(BaseModel):
    model: str
    messages: list[Message] = Field(min_length=1)
    system: str | None = None
    max_tokens: int = Field(default=4096, gt=0)
    # None = não enviar. Modelos Claude 5 rejeitam `temperature` (400:
    # "deprecated for this model"); quem precisa de amostragem específica
    # define explicitamente e o provider repassa.
    temperature: float | None = Field(default=None, ge=0, le=1)
    timeout_seconds: float = Field(default=120.0, gt=0)
    # Regra 2: quando presente, o provider DEVE devolver parsed validado
    response_schema: type[BaseModel] | None = None
    # Bloco ESTÁVEL entre chamadas do mesmo workflow (ex.: grounding do
    # repositório). Vai ANTES de `system` e é marcado para cache no fornecedor
    # quando ele suporta; onde não suporta, é apenas concatenado. Precisa ser
    # idêntico byte a byte entre chamadas para o cache bater — o que varia por
    # tarefa fica em `system` ou nas mensagens.
    cache_prefix: str | None = None
    # Ferramentas de exploração. Com response_schema, a resposta final
    # continua sendo a ferramenta de saída estruturada: o modelo termina
    # chamando-a. force_final obriga essa chamada (fim do loop de tool-use)
    # mantendo as definições — a API exige `tools` quando o histórico tem
    # tool_use/tool_result.
    tools: list[ToolSpec] = Field(default_factory=list)
    force_final: bool = False
    # Papel que faz a chamada ("judge", ...): o router pode ter bindings
    # próprios por papel. avoid_models: modelos que este papel NÃO deve usar
    # (ex.: o judge evitando o modelo que executou a tarefa); o router tenta
    # outro tier antes de desistir. Quem chama nunca escolhe modelo (regra 1).
    role: str | None = None
    avoid_models: list[str] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Entrada + saída, incluindo tokens lidos/escritos em cache: eles
        ocupam contexto e contam para o budget de tokens da tarefa."""
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.output_tokens
        )


class CompletionResult(BaseModel):
    text: str
    parsed: dict[str, Any] | None = None  # presente sse response_schema foi pedido
    tool_calls: list[ToolCall] = Field(default_factory=list)
    model: str
    provider: str
    usage: Usage
    cost_usd: float
    latency_ms: float
    stop_reason: str | None = None

    def parse_as(self, schema: type[T]) -> T:
        if self.parsed is None:
            raise StructuredOutputError(
                "Resultado sem saída estruturada.", provider=self.provider
            )
        return schema.model_validate(self.parsed)


class ModelPricing(BaseModel):
    """USD por milhão de tokens. Fonte: settings/env — verificar contra a
    página de preços do fornecedor, nunca confiar em valores commitados."""

    input_per_mtok: float = Field(gt=0)
    output_per_mtok: float = Field(gt=0)
    cache_read_per_mtok: float = 0.0
    cache_write_per_mtok: float = 0.0

    def cost(self, usage: Usage) -> float:
        return (
            usage.input_tokens * self.input_per_mtok
            + usage.output_tokens * self.output_per_mtok
            + usage.cache_read_tokens * self.cache_read_per_mtok
            + usage.cache_write_tokens * self.cache_write_per_mtok
        ) / 1_000_000


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------


class CircuitState(str, Enum):
    CLOSED = "closed"  # operação normal
    OPEN = "open"  # falhando rápido, sem tocar o fornecedor
    HALF_OPEN = "half_open"  # sonda: uma chamada de teste


class CircuitBreaker:
    """Um breaker por provider. Thread-safety via lock de asyncio —
    o estado é mutado apenas dentro do event loop."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        clock: Any = time.monotonic,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._state = CircuitState.CLOSED
        self._probe_in_flight = False
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        if (
            self._state == CircuitState.OPEN
            and self._opened_at is not None
            and self._clock() - self._opened_at >= self.recovery_timeout
        ):
            return CircuitState.HALF_OPEN
        return self._state

    async def before_call(self, provider: str) -> None:
        async with self._lock:
            if self.state == CircuitState.OPEN:
                raise CircuitOpenError(
                    f"Circuit breaker aberto para {provider}; "
                    f"retry em {self.recovery_timeout}s.",
                    provider=provider,
                )
            if self.state == CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    raise CircuitOpenError(
                        f"Circuit breaker em recuperação para {provider}; "
                        "sonda já em andamento.",
                        provider=provider,
                    )
                self._state = CircuitState.HALF_OPEN
                self._probe_in_flight = True

    async def on_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._opened_at = None
            self._state = CircuitState.CLOSED
            self._probe_in_flight = False

    async def on_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._state == CircuitState.HALF_OPEN or (
                self._failures >= self.failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
            self._probe_in_flight = False


# --------------------------------------------------------------------------
# Provider base — retry e breaker vivem AQUI, não nas subclasses
# --------------------------------------------------------------------------


class LLMProvider(ABC):
    name: str = "base"

    def __init__(
        self,
        pricing: dict[str, ModelPricing],
        breaker: CircuitBreaker | None = None,
        max_retries: int = 3,
        base_backoff_seconds: float = 1.0,
    ):
        self._pricing = pricing
        self._breaker = breaker or CircuitBreaker()
        self._max_retries = max_retries
        self._base_backoff = base_backoff_seconds

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Template method: breaker → tentativa → retry com backoff+jitter.
        Subclasses implementam apenas _do_complete."""
        last_error: ProviderError | None = None

        for attempt in range(self._max_retries + 1):
            budget = active_call_budget()
            estimate = self.estimate_request_usage(request)
            reservation = (
                budget.reserve(
                    estimate.total_tokens, self._reservation_cost_for(request.model, estimate)
                )
                if budget is not None else None
            )
            try:
                await self._breaker.before_call(self.name)
            except BaseException:
                if reservation is not None:
                    reservation.release()
                raise
            started = time.monotonic()
            try:
                try:
                    result = await asyncio.wait_for(
                        self._do_complete(request),
                        timeout=request.timeout_seconds,
                    )
                except BaseException:
                    if reservation is not None:
                        reservation.settle()
                    raise
                if reservation is not None:
                    reservation.settle(result.usage.total_tokens, result.cost_usd)
                await self._breaker.on_success()
                result.latency_ms = (time.monotonic() - started) * 1000
                usage = result.usage
                logger.info(
                    "llm_call provider=%s model=%s input=%d cache_write=%d cache_read=%d "
                    "output=%d cost_usd=%.5f latency_ms=%.0f",
                    self.name,
                    result.model,
                    usage.input_tokens,
                    usage.cache_write_tokens,
                    usage.cache_read_tokens,
                    usage.output_tokens,
                    result.cost_usd,
                    result.latency_ms,
                )
                return result

            except asyncio.TimeoutError:
                await self._breaker.on_failure()
                last_error = RetryableProviderError(
                    f"Timeout de {request.timeout_seconds}s.", provider=self.name
                )
            except RetryableProviderError as exc:
                await self._breaker.on_failure()
                last_error = exc
            except StructuredOutputError as exc:
                # O fornecedor respondeu, mas a geração veio truncada ou fora
                # do schema. Uma nova amostragem pode se recuperar e não deve
                # abrir o circuit breaker de disponibilidade.
                last_error = exc
            except NonRetryableProviderError:
                # não conta no breaker: o fornecedor está saudável,
                # a requisição é que está errada
                raise

            if attempt < self._max_retries:
                backoff = self._base_backoff * (2**attempt)
                await asyncio.sleep(backoff + random.uniform(0, backoff * 0.25))

        assert last_error is not None
        raise last_error

    def _cost_for(self, model: str, usage: Usage) -> float:
        pricing = self._pricing.get(model)
        if pricing is None:
            # custo desconhecido NÃO pode passar como zero: superestima
            # conservadoramente para o circuit breaker de budget disparar antes
            pricing = ModelPricing(input_per_mtok=30.0, output_per_mtok=150.0)
        return pricing.cost(usage)

    def estimate_request_cost(self, model: str, request: CompletionRequest) -> float:
        """Conservative reservation, including configured retries, not a billing cap."""
        return self._reservation_cost_for(model, self.estimate_request_usage(request)) * (
            self._max_retries + 1
        )

    def _reservation_cost_for(self, model: str, usage: Usage) -> float:
        pricing = self._pricing.get(model)
        if pricing is None:
            return self._cost_for(model, usage)
        # Cache creation may cost more than ordinary input. Reserve the highest
        # configured input rate without assuming the cache will be hit.
        input_rate = max(pricing.input_per_mtok, pricing.cache_read_per_mtok,
                         pricing.cache_write_per_mtok)
        return (usage.input_tokens * input_rate
                + usage.output_tokens * pricing.output_per_mtok) / 1_000_000

    @staticmethod
    def estimate_request_usage(request: CompletionRequest) -> Usage:
        """UTF-8 bytes plus framing headroom conservatively estimate token input.

        Include tool definitions, call arguments and results: growing tool history
        is part of every subsequent prompt. Output reserves the requested maximum.
        Vendor framing/tokenization may differ; actual usage always remains visible.
        """
        schema = request.response_schema.model_json_schema() if request.response_schema else {}
        text = json.dumps({
            "schema": schema,
            "system": request.system,
            "cache_prefix": request.cache_prefix,
            "messages": [message.model_dump() for message in request.messages],
            "tools": [tool.model_dump() for tool in request.tools],
        }, ensure_ascii=False)
        return Usage(input_tokens=len(text.encode("utf-8")) + 4096,
                     output_tokens=request.max_tokens)

    @staticmethod
    def _validate_structured(
        raw: dict[str, Any], schema: type[BaseModel], provider: str
    ) -> dict[str, Any]:
        try:
            return schema.model_validate(raw).model_dump(mode="json")
        except ValidationError as exc:
            # Modelos às vezes embrulham a saída em uma chave única
            # ({"parameters": {...}}, {"input": {...}}). Visto em produção com
            # Claude Sonnet 5 no judge. Desembrulhar é mais barato que repetir
            # a chamada — e a validação continua estrita no conteúdo.
            if len(raw) == 1:
                (inner,) = raw.values()
                if isinstance(inner, dict):
                    try:
                        return schema.model_validate(inner).model_dump(mode="json")
                    except ValidationError:
                        pass
            raise StructuredOutputError(
                f"Saída não valida contra {schema.__name__}: {exc}",
                provider=provider,
            ) from exc

    @abstractmethod
    async def _do_complete(self, request: CompletionRequest) -> CompletionResult:
        """Uma tentativa crua contra o fornecedor. Deve mapear erros HTTP
        para Retryable/NonRetryable. Sem retry aqui dentro."""
