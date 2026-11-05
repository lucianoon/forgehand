"""Keep a useful response possible without raising task/workflow allowances."""

import asyncio

import pytest

from app.infrastructure.llm_budget import BudgetAdmissionError, CallBudget, call_budget_scope
from app.providers.base import (
    CompletionRequest, CompletionResult, LLMProvider, Message, ModelPricing, Usage,
)


class RecordingProvider(LLMProvider):
    name = "recording"

    def __init__(self):
        super().__init__({"fake": ModelPricing(input_per_mtok=1, output_per_mtok=2)},
                         max_retries=0)
        self.calls = []

    async def _do_complete(self, request):
        self.calls.append(request)
        usage = Usage(input_tokens=100, output_tokens=1)
        return CompletionResult(text="ok", model=request.model, provider=self.name,
                                usage=usage, cost_usd=self._cost_for(request.model, usage),
                                latency_ms=0)


def request_with_input_estimate(tokens):
    request = CompletionRequest(model="fake", messages=[Message(role="user", content="")],
                                max_tokens=16384)
    overhead = LLMProvider.estimate_request_usage(request).input_tokens
    request.messages[0].content = "x" * (tokens - overhead)
    assert LLMProvider.estimate_request_usage(request).input_tokens == tokens
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt,spent,expected", [(47508, 40302, 12190), (56550, 38280, 5170)])
async def test_live_admission_cases_fit_output_without_increasing_budget(prompt, spent, expected):
    provider = RecordingProvider()
    request = request_with_input_estimate(prompt)
    budget = CallBudget(max_tokens=100000, max_cost_usd=1, tokens=spent)
    with call_budget_scope(budget):
        await provider.complete(request)
    assert provider.calls[0].max_tokens == expected
    assert request.max_tokens == 16384
    assert budget.max_tokens == 100000
    assert budget.tokens == spent + 101
    assert budget.reserved_tokens == 0
    assert budget.blocked_reason is None


@pytest.mark.asyncio
async def test_parent_reservations_and_unknown_usage_limit_output():
    provider = RecordingProvider()
    request = request_with_input_estimate(5000)
    parent = CallBudget(max_tokens=12000, max_cost_usd=1, tokens=1000,
                        unconfirmed_tokens=1000)
    pending = parent.reserve(3000, 0.01)
    child = CallBudget(max_tokens=100000, max_cost_usd=1)
    try:
        with call_budget_scope(parent), call_budget_scope(child):
            await provider.complete(request)
        assert provider.calls[0].max_tokens == 2000
        assert parent.reserved_tokens == 3000
        assert parent.unconfirmed_tokens == 1000
        assert child.tokens == 101
    finally:
        pending.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowance", [4999, 5000])
async def test_input_without_room_for_any_output_still_blocks(allowance):
    provider = RecordingProvider()
    budget = CallBudget(max_tokens=allowance, max_cost_usd=1)
    with call_budget_scope(budget), pytest.raises(BudgetAdmissionError):
        await provider.complete(request_with_input_estimate(5000))
    assert not provider.calls
    assert budget.tokens == budget.reserved_tokens == 0


@pytest.mark.asyncio
async def test_financial_allowance_caps_output_using_reservation_prices():
    provider = RecordingProvider()
    request = request_with_input_estimate(5000)
    # 5000 input tokens + 137.25 output tokens at the configured rates.
    budget = CallBudget(max_tokens=100000, max_cost_usd=(5000 + 137.25 * 2) / 1e6)
    with call_budget_scope(budget):
        await provider.complete(request)
    assert provider.calls[0].max_tokens == 137
    assert budget.cost_usd < budget.max_cost_usd


@pytest.mark.asyncio
async def test_financial_input_alone_cannot_fit_and_no_call_is_sent():
    provider = RecordingProvider()
    budget = CallBudget(max_tokens=100000, max_cost_usd=0.004)
    with call_budget_scope(budget), pytest.raises(BudgetAdmissionError):
        await provider.complete(request_with_input_estimate(5000))
    assert not provider.calls


@pytest.mark.asyncio
async def test_concurrent_children_see_reserved_allowance_before_sending():
    entered = asyncio.Event()
    finish = asyncio.Event()

    class HoldingProvider(RecordingProvider):
        async def _do_complete(self, request):
            if not entered.is_set():
                entered.set()
                await finish.wait()
            return await super()._do_complete(request)

    provider = HoldingProvider()
    parent = CallBudget(max_tokens=15000, max_cost_usd=1)
    request = request_with_input_estimate(5000).model_copy(update={"max_tokens": 4000})

    async def call():
        with call_budget_scope(parent), call_budget_scope(CallBudget(100000, 1)):
            return await provider.complete(request)

    first = asyncio.create_task(call())
    try:
        await entered.wait()
        assert parent.reserved_tokens == 9000
        await call()
        assert provider.calls[0].max_tokens == 1000
    finally:
        finish.set()
        await first
    assert sorted(item.max_tokens for item in provider.calls) == [1000, 4000]
    assert parent.tokens == 202
    assert parent.reserved_tokens == 0


@pytest.mark.asyncio
async def test_without_budget_the_original_request_limit_is_preserved():
    provider = RecordingProvider()
    request = request_with_input_estimate(5000)
    await provider.complete(request)
    assert provider.calls[0].max_tokens == 16384


@pytest.mark.asyncio
async def test_tool_loop_refits_response_after_reading_a_file(tmp_path):
    from app.agents.tools import ReadFileTool, ToolLoop
    from app.providers.base import ToolCall
    from app.providers.registry import ModelTier, ProviderRouter, TierBinding

    (tmp_path / "source.py").write_text("x" * 5000)

    class ReadingProvider(RecordingProvider):
        async def _do_complete(self, request):
            result = await super()._do_complete(request)
            if len(self.calls) == 1:
                return result.model_copy(update={"tool_calls": [
                    ToolCall(id="read", name="read_file", arguments={"path": "source.py"}),
                ]})
            return result

    provider = ReadingProvider()
    router = ProviderRouter({provider.name: provider}, {
        ModelTier.STANDARD: TierBinding(provider_name=provider.name, model="fake"),
    })
    loop = ToolLoop(router, [ReadFileTool(str(tmp_path))])
    request = CompletionRequest(model="", messages=[Message(role="user", content="read")],
                                max_tokens=16384)
    budget = CallBudget(20000, 1)
    with call_budget_scope(budget):
        result = await loop.run(None, request, token_ceiling=20000)
    assert result.result.text == "ok"
    assert len(provider.calls) == 2
    assert provider.calls[1].max_tokens < provider.calls[0].max_tokens < 16384
    assert budget.tokens == 202
    assert not budget.blocked_reason
    assert budget.reserved_tokens == 0


@pytest.mark.asyncio
async def test_cache_write_premium_still_bounds_financial_admission():
    provider = RecordingProvider()
    provider._pricing["fake"] = ModelPricing(input_per_mtok=1, output_per_mtok=2,
                                            cache_write_per_mtok=3)
    budget = CallBudget(100000, (5000 * 3 + 137.25 * 2) / 1e6)
    with call_budget_scope(budget):
        await provider.complete(request_with_input_estimate(5000))
    assert provider.calls[0].max_tokens == 137
