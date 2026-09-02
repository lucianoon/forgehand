"""Prompt caching: prefixo estável marcado para cache nos providers, usage de
cache lido e precificado, grounding compartilhado entre papéis."""

from __future__ import annotations

import json

import anthropic
import httpx
import pytest

from app.agents.grounding import (
    build_grounding_prefix,
    format_evidence_focus,
    format_repository_grounding,
)
from app.infrastructure.settings import Settings
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import CompletionRequest, Message, ModelPricing, Usage
from app.providers.openai_compatible import OpenAICompatibleProvider

PRICING = {
    "claude-sonnet-5": ModelPricing(
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cache_write_per_mtok=3.75,
        cache_read_per_mtok=0.30,
    ),
    "openai/gpt-4o-mini": ModelPricing(
        input_per_mtok=0.15, output_per_mtok=0.60, cache_read_per_mtok=0.075
    ),
}
PREFIX = "Grounding obrigatório do repositório:\n- repo_root: /repo\n" * 20
SYSTEM = "Você é o executor."


def _anthropic_client(handler):
    return anthropic.AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _anthropic_response(usage: dict[str, int]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": usage,
        },
    )


def _request(**overrides):
    base = dict(
        model="claude-sonnet-5",
        system=SYSTEM,
        messages=[Message(role="user", content="Tarefa: x")],
    )
    base.update(overrides)
    return CompletionRequest(**base)


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_marks_prefix_and_system_as_cache_breakpoints():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _anthropic_response(
            {
                "input_tokens": 20,
                "output_tokens": 5,
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 0,
            }
        )

    provider = AnthropicProvider(PRICING, client=_anthropic_client(handler))
    result = await provider.complete(_request(cache_prefix=PREFIX))

    system = seen["system"]
    assert isinstance(system, list) and len(system) == 2
    assert system[0] == {
        "type": "text",
        "text": PREFIX,
        "cache_control": {"type": "ephemeral"},
    }
    assert system[1]["text"] == SYSTEM
    assert system[1]["cache_control"] == {"type": "ephemeral"}
    # o user content não carrega o prefixo
    assert seen["messages"] == [{"role": "user", "content": "Tarefa: x"}]

    assert result.usage.cache_write_tokens == 1000
    assert result.usage.total_tokens == 20 + 1000 + 5
    expected = (20 * 3.0 + 1000 * 3.75 + 5 * 15.0) / 1e6
    assert result.cost_usd == pytest.approx(expected)


@pytest.mark.asyncio
async def test_anthropic_without_prefix_keeps_plain_system_string():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _anthropic_response({"input_tokens": 10, "output_tokens": 2})

    provider = AnthropicProvider(PRICING, client=_anthropic_client(handler))
    await provider.complete(_request())

    assert seen["system"] == SYSTEM


@pytest.mark.asyncio
async def test_anthropic_cache_read_is_priced_at_cache_rate():
    def handler(request: httpx.Request) -> httpx.Response:
        return _anthropic_response(
            {
                "input_tokens": 20,
                "output_tokens": 5,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 1000,
            }
        )

    provider = AnthropicProvider(PRICING, client=_anthropic_client(handler))
    result = await provider.complete(_request(cache_prefix=PREFIX))

    assert result.usage.cache_read_tokens == 1000
    expected = (20 * 3.0 + 1000 * 0.30 + 5 * 15.0) / 1e6
    assert result.cost_usd == pytest.approx(expected)


# --------------------------------------------------------------------------
# OpenAI-compatible / OpenRouter
# --------------------------------------------------------------------------


def _openai_response(usage: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "openai/gpt-4o-mini",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": usage,
        },
    )


@pytest.mark.asyncio
async def test_openai_compatible_emits_cache_control_blocks_when_supported():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _openai_response(
            {
                "prompt_tokens": 1020,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 1000},
            }
        )

    provider = OpenAICompatibleProvider(
        PRICING,
        base_url="https://router.test",
        provider_name="openrouter",
        supports_prompt_caching=True,
        client=httpx.AsyncClient(
            base_url="https://router.test", transport=httpx.MockTransport(handler)
        ),
    )
    result = await provider.complete(
        _request(model="openai/gpt-4o-mini", cache_prefix=PREFIX)
    )

    system_message = seen["messages"][0]
    assert system_message["role"] == "system"
    blocks = system_message["content"]
    assert blocks[0]["text"] == PREFIX
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["text"] == SYSTEM

    # cached_tokens já está dentro de prompt_tokens: não pode ser cobrado 2x
    assert result.usage.input_tokens == 20
    assert result.usage.cache_read_tokens == 1000
    assert result.usage.total_tokens == 1020 + 5
    expected = (20 * 0.15 + 1000 * 0.075 + 5 * 0.60) / 1e6
    assert result.cost_usd == pytest.approx(expected)


@pytest.mark.asyncio
async def test_openai_compatible_concatenates_prefix_when_caching_unsupported():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _openai_response({"prompt_tokens": 30, "completion_tokens": 5})

    provider = OpenAICompatibleProvider(
        PRICING,
        base_url="https://local.test",
        provider_name="local",
        supports_prompt_caching=False,
        client=httpx.AsyncClient(
            base_url="https://local.test", transport=httpx.MockTransport(handler)
        ),
    )
    result = await provider.complete(
        _request(model="openai/gpt-4o-mini", cache_prefix=PREFIX)
    )

    system_message = seen["messages"][0]
    assert system_message == {"role": "system", "content": f"{PREFIX}\n\n{SYSTEM}"}
    assert result.usage.cache_read_tokens == 0
    assert result.usage.input_tokens == 30


def test_settings_default_pricing_includes_cache_rates_for_claude():
    pricing = Settings().pricing
    for model in ("claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"):
        entry = pricing[model]
        assert entry.cache_write_per_mtok == pytest.approx(entry.input_per_mtok * 1.25)
        assert entry.cache_read_per_mtok == pytest.approx(entry.input_per_mtok * 0.10)
    assert Settings().openrouter_prompt_caching is True


def test_usage_total_tokens_counts_cached_tokens():
    usage = Usage(
        input_tokens=10, output_tokens=5, cache_read_tokens=100, cache_write_tokens=50
    )
    assert usage.total_tokens == 165


# --------------------------------------------------------------------------
# Grounding: prefixo compartilhado + foco por tarefa
# --------------------------------------------------------------------------

CONTEXT = {
    "repository_grounding": {
        "repo_root": "/repo",
        "require_citations": True,
        "top_level_entries": ["app"],
        "evidence": [
            {
                "id": f"E{i}",
                "path": f"app/m{i}.py",
                "line_start": 1,
                "line_end": 2,
                "excerpt": f"# m{i}",
            }
            for i in range(1, 13)
        ],
    }
}


def test_grounding_prefix_is_complete_and_role_agnostic():
    prefix = build_grounding_prefix(CONTEXT)
    assert prefix is not None
    # todas as evidências, não só as 8 primeiras: o prefixo é o mesmo para
    # qualquer tarefa/papel do workflow
    assert "[E12] app/m12.py:1-2" in prefix
    assert prefix == build_grounding_prefix(CONTEXT)
    assert format_repository_grounding(CONTEXT, evidence_ids=["E3"]) != prefix


def test_grounding_prefix_absent_without_evidence():
    assert build_grounding_prefix({}) is None
    assert build_grounding_prefix({"repository_grounding": {"evidence": []}}) is None


def test_evidence_focus_lists_task_ids_only():
    assert format_evidence_focus(["E1", "E3"]) == (
        "Evidências atribuídas a esta tarefa (fonte primária; as demais do "
        "grounding são contexto): [E1], [E3]"
    )
    assert format_evidence_focus([]) == ""
    assert format_evidence_focus(None) == ""


def test_otel_generation_span_carries_cache_token_attributes():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from app.infrastructure.tracing import OtelWorkflowTracer

    exporter = InMemorySpanExporter()
    tracer = OtelWorkflowTracer(
        "forgehand-test", span_processor=SimpleSpanProcessor(exporter)
    )
    tracer.record_generation(
        provider="anthropic",
        model="claude-sonnet-5",
        tier=2,
        latency_ms=1.0,
        input_tokens=20,
        output_tokens=5,
        cost_usd=0.001,
        cache_read_tokens=1000,
        cache_write_tokens=0,
    )
    span = exporter.get_finished_spans()[0]
    assert span.attributes["gen_ai.usage.cache_read.input_tokens"] == 1000
    assert span.attributes["gen_ai.usage.cache_creation.input_tokens"] == 0
