import pytest
from langgraph.checkpoint.memory import MemorySaver
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.graph.workflow import build_serde, build_workflow
from app.infrastructure.settings import Settings
from app.models.task import AgentTask, Capability, EvaluationResult
from app.infrastructure.tracing import (
    NOOP_TRACER,
    OtelWorkflowTracer,
    current_trace_id,
    tracing_context,
)
from app.providers.base import (
    CompletionRequest,
    CompletionResult,
    LLMProvider,
    Message,
    NonRetryableProviderError,
    Usage,
)
from app.providers.registry import ModelTier, ProviderRouter, TierBinding


class RecordingTracer:
    def __init__(self):
        self.generations = []

    def record_generation(self, **kwargs):
        self.generations.append(kwargs)


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, error=None):
        super().__init__(pricing={}, max_retries=0)
        self._error = error

    async def _do_complete(self, request):
        if self._error is not None:
            raise self._error
        return CompletionResult(
            text="ok",
            model=request.model,
            provider=self.name,
            usage=Usage(input_tokens=10, output_tokens=5),
            cost_usd=0.003,
            latency_ms=0,
        )


def make_router(provider, tracer):
    return ProviderRouter(
        providers={"fake": provider},
        bindings={ModelTier.STANDARD: TierBinding(provider_name="fake", model="m1")},
        tracer=tracer,
    )


def make_request():
    return CompletionRequest(model="", messages=[Message(role="user", content="oi")])


# --------------------------------------------------------------------------
# Instrumentação na porta única (ProviderRouter)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_records_generation_on_success():
    tracer = RecordingTracer()
    router = make_router(FakeProvider(), tracer)

    await router.complete(ModelTier.STANDARD, make_request())

    assert len(tracer.generations) == 1
    generation = tracer.generations[0]
    assert generation["provider"] == "fake"
    assert generation["model"] == "m1"
    assert generation["tier"] == int(ModelTier.STANDARD)
    assert generation["input_tokens"] == 10
    assert generation["output_tokens"] == 5
    assert generation["cost_usd"] == pytest.approx(0.003)


@pytest.mark.asyncio
async def test_router_records_generation_on_provider_error():
    tracer = RecordingTracer()
    error = NonRetryableProviderError("payload inválido", provider="fake")
    router = make_router(FakeProvider(error=error), tracer)

    with pytest.raises(NonRetryableProviderError):
        await router.complete(ModelTier.STANDARD, make_request())

    assert len(tracer.generations) == 1
    generation = tracer.generations[0]
    assert generation["error"] == "NonRetryableProviderError"
    assert generation["model"] == "m1"


@pytest.mark.asyncio
async def test_router_without_tracer_keeps_working():
    router = ProviderRouter(
        providers={"fake": FakeProvider()},
        bindings={ModelTier.STANDARD: TierBinding(provider_name="fake", model="m1")},
    )
    result = await router.complete(ModelTier.STANDARD, make_request())
    assert result.text == "ok"


# --------------------------------------------------------------------------
# Tracer OTel (exporter em memória injetado)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_otel_tracer_nests_generation_under_workflow_span():
    exporter = InMemorySpanExporter()
    tracer = OtelWorkflowTracer(
        "agent-forge-test", span_processor=SimpleSpanProcessor(exporter)
    )

    assert current_trace_id() is None
    with tracer.span("workflow", {"agent_forge.workflow_id": "wf-1"}):
        inner_trace_id = current_trace_id()
        assert inner_trace_id is not None
        tracer.record_generation(
            provider="fake",
            model="m1",
            tier=2,
            latency_ms=12.5,
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.003,
        )
    await tracer.shutdown()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) == {"workflow", "chat m1"}
    generation = spans["chat m1"]
    workflow = spans["workflow"]
    # mesmo trace, geração aninhada no span do workflow
    assert format(generation.context.trace_id, "032x") == inner_trace_id
    assert generation.parent.span_id == workflow.context.span_id
    assert generation.attributes["gen_ai.usage.input_tokens"] == 10
    assert generation.attributes["agent_forge.cost_usd"] == pytest.approx(0.003)


@pytest.mark.asyncio
async def test_otel_tracer_marks_errors():
    exporter = InMemorySpanExporter()
    tracer = OtelWorkflowTracer(
        "agent-forge-test", span_processor=SimpleSpanProcessor(exporter)
    )
    tracer.record_generation(
        provider="fake",
        model="m1",
        tier=2,
        latency_ms=1.0,
        error="RetryableProviderError",
    )
    await tracer.shutdown()

    span = exporter.get_finished_spans()[0]
    assert span.attributes["error.type"] == "RetryableProviderError"
    assert not span.status.is_ok


# --------------------------------------------------------------------------
# Propagação do contexto através do fan-out do LangGraph
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_id_reaches_task_attempts_through_parallel_fanout():
    class TwoTaskPlanner:
        async def create_plan(self, request, context):
            return [
                AgentTask(
                    title=f"t{i}",
                    description="d",
                    capability=Capability.BACKEND,
                    acceptance_criteria=["ok"],
                )
                for i in range(2)
            ]

    class Registry:
        def select(self, task):
            return self

        async def execute(self, task, context):
            return {
                "result": {"ok": True},
                "agent": "fake",
                "model": "fake",
                "tokens": 1,
                "cost_usd": 0.0,
            }

    class ApprovingJudge:
        async def evaluate(self, task, context):
            return EvaluationResult(
                task_id=task.id, approved=True, score=1, criteria_scores={"ok": 1}
            )

    class Memory:
        async def load_context(self, project_id, request=""):
            return {}

        async def persist(self, state):
            return None

    exporter = InMemorySpanExporter()
    tracer = OtelWorkflowTracer(
        "agent-forge-test", span_processor=SimpleSpanProcessor(exporter)
    )
    app = build_workflow(
        TwoTaskPlanner(),
        Registry(),
        ApprovingJudge(),
        Memory(),
        MemorySaver(serde=build_serde()),
    )

    with tracer.span("workflow"):
        workflow_trace_id = current_trace_id()
        output = await app.ainvoke(
            {
                "request": "teste",
                "project_id": "p",
                "workflow_id": "wf-trace",
                "owner_client_id": "c",
            },
            {"configurable": {"thread_id": "wf-trace"}},
        )
    await tracer.shutdown()

    # as duas branches paralelas herdaram o contexto do span do workflow
    attempts = [attempt for task in output["plan"] for attempt in task.attempts]
    assert len(attempts) == 2
    assert all(attempt.trace_id == workflow_trace_id for attempt in attempts)


# --------------------------------------------------------------------------
# Contexto de lifecycle
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracing_context_defaults_to_noop():
    settings = Settings(repository_grounding_enabled=False)
    async with tracing_context(settings) as tracer:
        assert tracer is NOOP_TRACER
        # no-op aceita a interface inteira sem efeito
        with tracer.span("workflow"):
            assert current_trace_id() is None
