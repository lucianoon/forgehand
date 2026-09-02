"""Tracing de workflows e chamadas de LLM — OTel/Langfuse (Fase 7).

Uma única integração OpenTelemetry serve qualquer backend OTLP, incluindo o
Langfuse (endpoint /api/public/otel). Endpoint e credenciais NUNCA passam
pelas Settings: o exporter OTLP lê as variáveis padrão do OTel direto do
ambiente (OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_HEADERS) — settings
são logáveis, segredos não.

Pontos de instrumentação:
- ProviderRouter.complete — a porta única de LLM (regra 1) registra um span
  gen_ai por chamada: modelo, tier, tokens, custo, latência e erro;
- WorkflowService._invoke_job — span raiz por job; os spans de LLM aninham
  automaticamente via contextvars do OTel, inclusive no fan-out paralelo
  (asyncio copia o contexto na criação de cada task);
- execute_task — grava trace_id no TaskAttempt via current_trace_id(),
  fechando a correlação tentativa → trace prometida no modelo.

Tracing é OBSERVADOR: NoopTracer é o default e nenhuma falha de export
afeta o workflow (BatchSpanProcessor exporta fora do caminho da request).
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager, contextmanager, nullcontext
from typing import Any, AsyncGenerator, ContextManager, Iterator

from app.infrastructure.settings import Settings


def current_trace_id() -> str | None:
    """Trace id hex do span ativo, ou None sem OTel/span — seguro de chamar
    de qualquer camada: não depende de nada interno e degrada para None."""
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    context = trace.get_current_span().get_span_context()
    if context.trace_id == 0:
        return None
    return format(context.trace_id, "032x")


class WorkflowTracer:
    """Base no-op — a interface que router e service enxergam."""

    def span(
        self, name: str, attributes: dict[str, Any] | None = None
    ) -> ContextManager[None]:
        return nullcontext()

    def record_generation(
        self,
        *,
        provider: str,
        model: str,
        tier: int,
        latency_ms: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        error: str | None = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        return None

    async def shutdown(self) -> None:
        return None


NOOP_TRACER = WorkflowTracer()


class OtelWorkflowTracer(WorkflowTracer):
    """Spans OTel com atributos da semconv gen_ai. O span_processor é
    injetável (mesmo padrão dos clients): testes usam exporter em memória,
    produção usa BatchSpanProcessor + OTLP lendo o ambiente padrão."""

    def __init__(self, service_name: str, span_processor: Any | None = None):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name})
        )
        if span_processor is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            span_processor = BatchSpanProcessor(OTLPSpanExporter())
        provider.add_span_processor(span_processor)
        self._provider = provider
        self._tracer = provider.get_tracer("forgehand")

    @contextmanager
    def _span(
        self, name: str, attributes: dict[str, Any] | None = None
    ) -> Iterator[None]:
        with self._tracer.start_as_current_span(name, attributes=attributes or {}):
            yield

    def span(
        self, name: str, attributes: dict[str, Any] | None = None
    ) -> ContextManager[None]:
        return self._span(name, attributes)

    def record_generation(
        self,
        *,
        provider: str,
        model: str,
        tier: int,
        latency_ms: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        error: str | None = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        from opentelemetry.trace import Status, StatusCode

        end = time.time_ns()
        start = end - int(latency_ms * 1_000_000)
        span = self._tracer.start_span(
            f"chat {model}",
            start_time=start,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.system": provider,
                "gen_ai.request.model": model,
                "gen_ai.usage.input_tokens": input_tokens,
                "gen_ai.usage.output_tokens": output_tokens,
                "gen_ai.usage.cache_read.input_tokens": cache_read_tokens,
                "gen_ai.usage.cache_creation.input_tokens": cache_write_tokens,
                "forgehand.tier": tier,
                "forgehand.cost_usd": cost_usd,
            },
        )
        if error is not None:
            span.set_attribute("error.type", error)
            span.set_status(Status(StatusCode.ERROR, error))
        span.end(end_time=end)

    async def shutdown(self) -> None:
        self._provider.shutdown()


@asynccontextmanager
async def tracing_context(
    settings: Settings,
) -> AsyncGenerator[WorkflowTracer, None]:
    """Mesmo padrão do checkpointer/memória: recurso com lifecycle — o
    shutdown drena o batch de spans pendentes."""
    if settings.tracing_backend == "otlp":
        # import tardio dentro do tracer: dependência opcional (extra [otel])
        tracer: WorkflowTracer = OtelWorkflowTracer(settings.tracing_service_name)
    else:
        tracer = NOOP_TRACER
    try:
        yield tracer
    finally:
        await tracer.shutdown()
