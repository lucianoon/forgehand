"""Parâmetros de amostragem: `temperature` só vai ao fornecedor quando pedido.
Modelos Claude 5 rejeitam o campo (400 "deprecated for this model")."""

from __future__ import annotations

import json

import anthropic
import httpx
import pytest

from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import CompletionRequest, Message, ModelPricing
from app.providers.openai_compatible import OpenAICompatibleProvider

PRICING = {
    "claude-sonnet-5": ModelPricing(input_per_mtok=3.0, output_per_mtok=15.0),
    "openai/gpt-4o-mini": ModelPricing(input_per_mtok=0.15, output_per_mtok=0.60),
}


def _request(**overrides) -> CompletionRequest:
    base = dict(model="claude-sonnet-5", messages=[Message(role="user", content="oi")])
    base.update(overrides)
    return CompletionRequest(**base)


def _anthropic(seen: dict) -> AnthropicProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "m",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    return AnthropicProvider(
        PRICING,
        client=anthropic.AsyncAnthropic(
            api_key="test",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ),
    )


def _openai(seen: dict) -> OpenAICompatibleProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-4o-mini",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    return OpenAICompatibleProvider(
        PRICING,
        base_url="https://router.test",
        client=httpx.AsyncClient(
            base_url="https://router.test", transport=httpx.MockTransport(handler)
        ),
    )


def test_temperature_defaults_to_unset():
    assert (
        CompletionRequest(
            model="m", messages=[Message(role="user", content="x")]
        ).temperature
        is None
    )


@pytest.mark.asyncio
async def test_anthropic_omits_temperature_unless_requested():
    seen: dict = {}
    await _anthropic(seen).complete(_request())
    assert "temperature" not in seen

    seen.clear()
    await _anthropic(seen).complete(_request(temperature=0.5))
    assert seen["temperature"] == 0.5


@pytest.mark.asyncio
async def test_openai_compatible_omits_temperature_unless_requested():
    seen: dict = {}
    await _openai(seen).complete(_request(model="openai/gpt-4o-mini"))
    assert "temperature" not in seen

    seen.clear()
    await _openai(seen).complete(_request(model="openai/gpt-4o-mini", temperature=0.0))
    assert seen["temperature"] == 0.0


def test_structured_output_unwraps_single_key_wrapper():
    from pydantic import BaseModel

    from app.providers.base import LLMProvider, StructuredOutputError

    class Out(BaseModel):
        approved: bool
        score: float

    ok = LLMProvider._validate_structured(
        {"parameters": {"approved": True, "score": 0.9}}, Out, "fake"
    )
    assert ok == {"approved": True, "score": 0.9}
    # conteúdo inválido continua falhando mesmo embrulhado
    with pytest.raises(StructuredOutputError):
        LLMProvider._validate_structured({"input": {"approved": "talvez"}}, Out, "fake")
    # duas chaves no topo não são um embrulho
    with pytest.raises(StructuredOutputError):
        LLMProvider._validate_structured(
            {"parameters": {"approved": True, "score": 1}, "extra": 1}, Out, "fake"
        )
