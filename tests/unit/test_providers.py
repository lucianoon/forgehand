import json
import sys

sys.path.insert(0, ".")
import httpx
import anthropic
import pytest
from app.api.container import build_provider_router
from pydantic import BaseModel
from app.infrastructure.settings import Settings
from app.providers.base import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    CompletionRequest,
    Message,
    ModelPricing,
    RetryableProviderError,
    StructuredOutputError,
)
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.registry import ModelTier, ProviderRouter, TierBinding

PRICING = {"claude-sonnet-5": ModelPricing(input_per_mtok=3.0, output_per_mtok=15.0)}


class PlanOut(BaseModel):
    tasks: list[str]
    rationale: str


def anthropic_mock(handler):
    transport = httpx.MockTransport(handler)
    return anthropic.AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=transport),
    )


def msg_response(content, model="claude-sonnet-5", in_tok=1000, out_tok=500):
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": "end_turn",
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        },
    )


@pytest.mark.asyncio
async def test_providers():
    req = CompletionRequest(
        model="claude-sonnet-5", messages=[Message(role="user", content="oi")]
    )

    # ---- 1. Texto + custo calculado pela tabela injetada ----
    def h1(request):
        return msg_response([{"type": "text", "text": "resposta"}])

    p = AnthropicProvider(PRICING, client=anthropic_mock(h1), base_backoff_seconds=0.01)
    r = await p.complete(req)
    assert r.text == "resposta"
    expected = (1000 * 3.0 + 500 * 15.0) / 1e6
    assert abs(r.cost_usd - expected) < 1e-12, r.cost_usd
    assert r.latency_ms > 0
    print(f"1. custo OK — {r.cost_usd:.6f} USD pela tabela injetada")

    # ---- 2. Saida estruturada via tool use forcado + validacao ----
    def h2(request):
        body = json.loads(request.content)
        assert body["tool_choice"] == {"type": "tool", "name": "emit_structured_output"}
        return msg_response(
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "emit_structured_output",
                    "input": {"tasks": ["backend", "testes"], "rationale": "mvp"},
                }
            ]
        )

    p2 = AnthropicProvider(
        PRICING, client=anthropic_mock(h2), base_backoff_seconds=0.01
    )
    r2 = await p2.complete(req.model_copy(update={"response_schema": PlanOut}))
    plan = r2.parse_as(PlanOut)
    assert plan.tasks == ["backend", "testes"]
    print("2. structured output OK — tool_choice forçado, Pydantic validado")

    # ---- 3. Schema invalido -> StructuredOutputError com retry limitado ----
    calls3 = {"n": 0}

    def h3(request):
        calls3["n"] += 1
        return msg_response(
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "emit_structured_output",
                    "input": {"tasks": "nao-e-lista"},
                }
            ]
        )

    p3 = AnthropicProvider(
        PRICING,
        client=anthropic_mock(h3),
        max_retries=1,
        base_backoff_seconds=0.01,
    )
    try:
        await p3.complete(req.model_copy(update={"response_schema": PlanOut}))
        raise SystemExit("FALHOU: aceitou schema invalido")
    except StructuredOutputError:
        pass
    assert calls3["n"] == 2
    print("3. schema inválido OK — retry limitado antes da falha final")

    # ---- 4. Retry: 429 -> 500 -> sucesso ----
    calls4 = {"n": 0}

    def h4(request):
        calls4["n"] += 1
        if calls4["n"] == 1:
            return httpx.Response(
                429,
                json={
                    "error": {
                        "type": "rate_limit_error",
                        "message": "slow down",
                    }
                },
            )
        if calls4["n"] == 2:
            return httpx.Response(
                500, json={"error": {"type": "api_error", "message": "oops"}}
            )
        return msg_response([{"type": "text", "text": "ok"}])

    p4 = AnthropicProvider(
        PRICING, client=anthropic_mock(h4), base_backoff_seconds=0.01
    )
    r4 = await p4.complete(req)
    assert r4.text == "ok" and calls4["n"] == 3
    print("4. retry OK — 429 e 500 re-tentados com backoff, 3ª tentativa passou")

    # ---- 5. Circuit breaker: abre, rejeita rapido, half-open recupera ----
    now = {"t": 0.0}
    breaker = CircuitBreaker(
        failure_threshold=2, recovery_timeout_seconds=30, clock=lambda: now["t"]
    )
    calls5 = {"n": 0}

    def h5(request):
        calls5["n"] += 1
        return httpx.Response(
            500, json={"error": {"type": "api_error", "message": "down"}}
        )

    p5 = AnthropicProvider(
        PRICING,
        breaker=breaker,
        max_retries=1,
        base_backoff_seconds=0.01,
        client=anthropic_mock(h5),
    )
    try:
        await p5.complete(req)
    except RetryableProviderError:
        pass
    assert breaker.state == CircuitState.OPEN
    hit_before = calls5["n"]
    try:
        await p5.complete(req)
        raise SystemExit("FALHOU: breaker aberto deixou passar")
    except CircuitOpenError:
        pass
    assert calls5["n"] == hit_before, "aberto NAO pode tocar o fornecedor"
    now["t"] = 31.0  # janela de recuperacao
    assert breaker.state == CircuitState.HALF_OPEN

    def h5b(request):
        return msg_response([{"type": "text", "text": "voltou"}])

    p5._client = anthropic_mock(h5b)
    r5 = await p5.complete(req)
    assert r5.text == "voltou" and breaker.state == CircuitState.CLOSED
    print("5. circuit breaker OK — open falha rápido, half-open sonda, sucesso fecha")

    # ---- 6. OpenAI-compatible: strict schema e fallback json_object ----
    def h6(request):
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        strict_schema = body["response_format"]["json_schema"]["schema"]
        assert strict_schema["additionalProperties"] is False
        assert strict_schema["required"] == ["tasks", "rationale"]
        assert body["provider"] == {"require_parameters": True}
        assert body["plugins"] == [{"id": "response-healing"}]
        return httpx.Response(
            200,
            json={
                "model": "local-model",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"tasks": ["a"], "rationale": "r"})
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    oc = OpenAICompatibleProvider(
        {},
        base_url="http://local",
        require_parameters=True,
        response_healing=True,
        client=httpx.AsyncClient(
            base_url="http://local", transport=httpx.MockTransport(h6)
        ),
        base_backoff_seconds=0.01,
    )
    r6 = await oc.complete(
        CompletionRequest(
            model="local-model",
            messages=[Message(role="user", content="oi")],
            response_schema=PlanOut,
        )
    )
    assert r6.parse_as(PlanOut).tasks == ["a"]

    def h6b(request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert (
            "schema" in body["messages"][0]["content"].lower()
            or "json" in body["messages"][0]["content"]
        )
        return httpx.Response(
            200,
            json={
                "model": "ollama",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"tasks": ["b"], "rationale": "r"})
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    oc2 = OpenAICompatibleProvider(
        {},
        base_url="http://local",
        supports_json_schema=False,
        provider_name="ollama",
        client=httpx.AsyncClient(
            base_url="http://local", transport=httpx.MockTransport(h6b)
        ),
        base_backoff_seconds=0.01,
    )
    r6b = await oc2.complete(
        CompletionRequest(
            model="ollama",
            messages=[Message(role="user", content="oi")],
            response_schema=PlanOut,
        )
    )
    assert r6b.parse_as(PlanOut).tasks == ["b"]
    print("6. openai-compatible OK — strict json_schema e fallback para locais")

    # ---- 7. Registry: agente nao escolhe modelo; escalonamento controlado ----
    def h7(request):
        body = json.loads(request.content)
        return msg_response(
            [{"type": "text", "text": f"model={body['model']}"}], model=body["model"]
        )

    ap = AnthropicProvider(
        PRICING, client=anthropic_mock(h7), base_backoff_seconds=0.01
    )
    router = ProviderRouter(
        providers={"anthropic": ap},
        bindings={
            ModelTier.STANDARD: TierBinding(
                provider_name="anthropic", model="claude-sonnet-5"
            ),
            ModelTier.STRONG: TierBinding(
                provider_name="anthropic", model="claude-opus-5"
            ),
        },
    )
    r7 = await router.complete(
        None,
        CompletionRequest(
            model="IGNORADO", messages=[Message(role="user", content="oi")]
        ),
    )
    assert "claude-sonnet-5" in r7.text, (
        "default = STANDARD, model do request ignorado"
    )
    tier = router.escalate(ModelTier.STANDARD)
    assert tier == ModelTier.STRONG
    r7b = await router.complete(
        tier,
        CompletionRequest(model="x", messages=[Message(role="user", content="oi")]),
    )
    assert "claude-opus-5" in r7b.text
    # FAST sem binding cai para STANDARD, nunca sobe sozinho
    _, m = router.resolve(ModelTier.FAST)
    assert m == "claude-sonnet-5"
    print(
        "7. registry OK — tier default STANDARD, escalate explícito, FAST degrada p/ baixo"
    )

    print("\n7/7 cenarios de providers OK")


@pytest.mark.asyncio
async def test_circuit_breaker_allows_only_one_half_open_probe():
    now = {"value": 0.0}
    breaker = CircuitBreaker(
        failure_threshold=1,
        recovery_timeout_seconds=1,
        clock=lambda: now["value"],
    )
    await breaker.on_failure()
    now["value"] = 2.0

    await breaker.before_call("provider")
    with pytest.raises(CircuitOpenError, match="sonda já em andamento"):
        await breaker.before_call("provider")

    await breaker.on_success()
    assert breaker.state == CircuitState.CLOSED


def test_build_provider_router_supports_openrouter_defaults():
    settings = Settings(llm_provider_backend="openrouter")
    router = build_provider_router(
        settings,
        openrouter_client=httpx.AsyncClient(
            base_url="https://openrouter.ai/api",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "model": "openai/gpt-4o-mini",
                        "choices": [
                            {
                                "message": {
                                    "content": '{"tasks":["a"],"rationale":"r"}'
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    },
                )
            ),
        ),
    )

    provider, model = router.resolve(ModelTier.STANDARD)
    assert provider.name == "openrouter"
    assert model == "openai/gpt-4o-mini"
