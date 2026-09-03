"""Direct OpenAI contracts; fake credentials and no external requests."""

import json

import httpx
import pytest
from pydantic import BaseModel

from app.api.container import build_provider_router
from app.infrastructure.settings import Settings
from app.providers.base import CompletionRequest, Message, ToolSpec
from app.providers.registry import ModelTier


MODEL = "gpt-4.1-mini-2025-04-14"


@pytest.mark.asyncio
async def test_openai_http_timeout_uses_the_completion_budget():
    def handle(request):
        assert request.extensions["timeout"]["read"] == 37.0
        return httpx.Response(
            200,
            json={
                "model": MODEL,
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://api.openai.com", transport=httpx.MockTransport(handle)
    ) as client:
        router = build_provider_router(settings(), openai_client=client)
        await router.complete(
            ModelTier.STANDARD,
            CompletionRequest(
                model=MODEL,
                messages=[Message(role="user", content="test")],
                timeout_seconds=37,
            ),
        )


def test_real_executor_schema_normalizes_discriminated_operations():
    from app.agents.executor import ExecutionOutput
    from app.providers.openai_compatible import _strict_json_schema

    original = ExecutionOutput.model_json_schema()
    schema = _strict_json_schema(original)
    operations = schema["properties"]["operations"]["items"]
    assert "oneOf" not in operations
    assert "discriminator" not in operations
    assert operations["anyOf"] == original["properties"]["operations"]["items"]["oneOf"]
    assert "oneOf" in original["properties"]["operations"]["items"]
    for name, tag in (
        ("CreateFile", "create"),
        ("ReplaceInFile", "replace"),
        ("DeleteFile", "delete"),
    ):
        assert schema["$defs"][name]["properties"]["op"]["const"] == tag
        assert "op" in schema["$defs"][name]["required"]
        assert schema["$defs"][name]["additionalProperties"] is False


def test_real_planner_schema_omits_defaults_in_strict_output():
    from app.agents.planner import PlanOutput
    from app.providers.openai_compatible import _strict_json_schema

    schema = _strict_json_schema(PlanOutput.model_json_schema())

    def check(node):
        if isinstance(node, dict):
            assert "default" not in node
            for child in node.values():
                check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)

    check(schema)


class Answer(BaseModel):
    result: str
    note: str | None = None


def settings(**kwargs):
    return Settings(_env_file=None, llm_provider_backend="openai", **kwargs)


def test_openai_defaults_use_priced_pinned_model_and_keep_factory_disabled():
    config = settings()
    assert not config.factory_mode_enabled
    for binding in config.tier_bindings.values():
        assert binding.provider_name == "openai"
        assert binding.model == MODEL
        assert binding.model in config.pricing
    assert config.pricing[MODEL].cache_read_per_mtok == 0.1
    assert Settings(_env_file=None).llm_provider_backend == "anthropic"


def test_openai_explicit_tier_bindings_are_preserved():
    config = settings(
        tier_bindings_json=json.dumps(
            {"2": {"provider_name": "openai", "model": "operator-selected-model"}}
        )
    )
    assert config.tier_bindings[ModelTier.STANDARD].model == "operator-selected-model"


@pytest.mark.parametrize("key", [None, "", "   "])
def test_openai_missing_key_fails_without_borrowing_another_provider(monkeypatch, key):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if key is not None:
        monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-test-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-secret")
    with pytest.raises(ValueError, match="OPENAI_API_KEY is required"):
        build_provider_router(settings())


@pytest.mark.asyncio
async def test_openai_credentials_and_destination_are_isolated(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-test-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unapproved.invalid")
    config = settings(openrouter_base_url="https://router.invalid")
    router = build_provider_router(config)
    provider, model = router.resolve(ModelTier.STANDARD)
    try:
        assert provider.name == "openai"
        assert model == MODEL
        assert str(provider._client.base_url) == "https://api.openai.com"
        assert provider._client.headers["Authorization"] == "Bearer openai-test-secret"
        assert "HTTP-Referer" not in provider._client.headers
        assert "X-Title" not in provider._client.headers
        assert "openai-test-secret" not in config.model_dump_json()
    finally:
        await provider._client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", [False, True])
async def test_openai_structured_output_tools_cache_and_cost_contract(
    monkeypatch, tools
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen = []

    def handle(request):
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        payload = json.loads(request.content)
        seen.append(payload)
        assert payload["model"] == MODEL
        assert payload["max_completion_tokens"] == 250
        assert payload["store"] is False
        for unsupported in ("max_tokens", "provider", "plugins", "temperature"):
            assert unsupported not in payload
        assert payload["messages"][0] == {
            "role": "system",
            "content": "stable prefix\n\nReturn JSON",
        }
        if tools:
            function = payload["tools"][0]["function"]
            assert function["strict"] is True
            assert (
                payload["tool_choice"]["function"]["name"] == "emit_structured_output"
            )
            schema = function["parameters"]
            message = {
                "tool_calls": [
                    {
                        "id": "call-final",
                        "type": "function",
                        "function": {
                            "name": "emit_structured_output",
                            "arguments": '{"result":"ok","note":null}',
                        },
                    }
                ]
            }
        else:
            schema = payload["response_format"]["json_schema"]["schema"]
            message = {"content": '{"result":"ok","note":null}'}
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {"result", "note"}
        return httpx.Response(
            200,
            json={
                "model": MODEL,
                "choices": [{"message": message, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "prompt_tokens_details": {"cached_tokens": 400},
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="https://api.openai.com", transport=httpx.MockTransport(handle)
    ) as client:
        router = build_provider_router(settings(), openai_client=client)
        result = await router.complete(
            ModelTier.STANDARD,
            CompletionRequest(
                model="ignored-agent-model",
                messages=[Message(role="user", content="Produce an answer")],
                response_schema=Answer,
                system="Return JSON",
                cache_prefix="stable prefix",
                max_tokens=250,
                tools=[ToolSpec(name="read_file", description="Read workspace file")]
                if tools
                else [],
                force_final=tools,
            ),
        )
    assert len(seen) == 1
    assert result.parsed == {"result": "ok", "note": None}
    assert result.provider == "openai"
    assert result.usage.input_tokens == 600
    assert result.usage.cache_read_tokens == 400
    assert result.usage.total_tokens == 1100
    assert result.cost_usd == pytest.approx((600 * 0.4 + 400 * 0.1 + 100 * 1.6) / 1e6)


@pytest.mark.asyncio
async def test_openrouter_never_uses_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-secret")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    router = build_provider_router(
        Settings(_env_file=None, llm_provider_backend="openrouter")
    )
    provider, _ = router.resolve(ModelTier.STANDARD)
    try:
        assert "Authorization" not in provider._client.headers
    finally:
        await provider._client.aclose()
