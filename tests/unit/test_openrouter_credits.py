from __future__ import annotations

import json

import httpx
import pytest

from app.providers.base import CompletionRequest, Message, NonRetryableProviderError
from app.providers.openai_compatible import (
    OpenAICompatibleProvider,
    affordable_completion_tokens,
)


def test_affordable_completion_tokens_reads_openrouter_402():
    body = (
        '{"error":{"message":"You requested up to 16384 tokens, but can only '
        'afford 11989.","code":402}}'
    )
    assert affordable_completion_tokens(body, 16384) == 11989
    assert affordable_completion_tokens(body, 8192) is None
    assert affordable_completion_tokens("nope", 16384) is None


@pytest.mark.asyncio
async def test_openrouter_402_retries_with_affordable_max_tokens():
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["max_tokens"])
        if body["max_tokens"] > 12000:
            return httpx.Response(
                402,
                json={
                    "error": {
                        "message": (
                            "This request requires more credits, or fewer "
                            "max_tokens. You requested up to 16384 tokens, "
                            "but can only afford 11989."
                        ),
                        "code": 402,
                        "metadata": {"limit_source": "openrouter_credits"},
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-4o-mini",
                "choices": [
                    {"message": {"content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = OpenAICompatibleProvider(
        {},
        base_url="https://openrouter.test/api",
        client=httpx.AsyncClient(
            base_url="https://openrouter.test/api",
            transport=httpx.MockTransport(handler),
        ),
    )
    result = await provider.complete(
        CompletionRequest(
            model="openai/gpt-4o-mini",
            messages=[Message(role="user", content="oi")],
            max_tokens=16384,
        )
    )
    assert result.text == "ok"
    assert seen == [16384, 11989]


@pytest.mark.asyncio
async def test_openrouter_402_without_affordable_budget_stays_non_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text="insufficient credits")

    provider = OpenAICompatibleProvider(
        {},
        base_url="https://openrouter.test/api",
        client=httpx.AsyncClient(
            base_url="https://openrouter.test/api",
            transport=httpx.MockTransport(handler),
        ),
    )
    with pytest.raises(NonRetryableProviderError) as exc:
        await provider.complete(
            CompletionRequest(
                model="openai/gpt-4o-mini",
                messages=[Message(role="user", content="oi")],
                max_tokens=8192,
            )
        )
    assert exc.value.status_code == 402
