"""Judge independente do executor: bindings por papel e avoid_models no router,
registro de independência na avaliação, quórum em tarefas críticas."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.agents.judge import LLMJudge
from app.infrastructure.settings import Settings
from app.models.task import AgentTask, Capability, TaskAttempt, TaskStatus
from app.providers.base import (
    CompletionRequest,
    CompletionResult,
    LLMProvider,
    Message,
    ModelPricing,
    Usage,
)
from app.providers.registry import ModelTier, ProviderRouter, TierBinding


class EchoProvider(LLMProvider):
    """Devolve o modelo pedido como texto: mostra qual binding o router usou."""

    def __init__(self, name: str) -> None:
        super().__init__({}, max_retries=0)
        self.name = name

    async def _do_complete(self, request: CompletionRequest) -> CompletionResult:
        return CompletionResult(
            text=request.model,
            parsed=None,
            model=request.model,
            provider=self.name,
            usage=Usage(input_tokens=1, output_tokens=1),
            cost_usd=0.0,
            latency_ms=0.0,
        )


def _router(role_bindings=None) -> ProviderRouter:
    return ProviderRouter(
        providers={
            "anthropic": EchoProvider("anthropic"),
            "openai": EchoProvider("openai"),
        },
        bindings={
            ModelTier.FAST: TierBinding(provider_name="anthropic", model="haiku"),
            ModelTier.STANDARD: TierBinding(provider_name="anthropic", model="sonnet"),
            ModelTier.STRONG: TierBinding(provider_name="anthropic", model="opus"),
        },
        role_bindings=role_bindings,
    )


def _req(**overrides) -> CompletionRequest:
    base = dict(model="", messages=[Message(role="user", content="x")])
    base.update(overrides)
    return CompletionRequest(**base)


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------


def test_role_bindings_override_only_declared_tiers():
    router = _router(
        {
            "judge": {
                ModelTier.STANDARD: TierBinding(provider_name="openai", model="gpt")
            }
        }
    )
    assert router.resolve(ModelTier.STANDARD)[1] == "sonnet"
    provider, model = router.resolve(ModelTier.STANDARD, role="judge")
    assert (provider.name, model) == ("openai", "gpt")
    # tier sem binding próprio do papel cai na tabela padrão
    assert router.resolve(ModelTier.STRONG, role="judge")[1] == "opus"
    assert router.resolve(ModelTier.STANDARD, role="desconhecido")[1] == "sonnet"


def test_avoid_models_prefers_same_tier_then_up_then_down():
    router = _router()
    assert router.resolve(ModelTier.STANDARD, avoid_models=["sonnet"])[1] == "opus"
    assert (
        router.resolve(ModelTier.STANDARD, avoid_models=["sonnet", "opus"])[1]
        == "haiku"
    )
    # sem alternativa: resolução normal (melhor esforço)
    assert (
        router.resolve(ModelTier.STANDARD, avoid_models=["sonnet", "opus", "haiku"])[1]
        == "sonnet"
    )
    # com binding de papel no mesmo tier, nem precisa escalar
    judged = _router(
        {
            "judge": {
                ModelTier.STANDARD: TierBinding(provider_name="openai", model="gpt")
            }
        }
    )
    assert (
        judged.resolve(ModelTier.STANDARD, role="judge", avoid_models=["sonnet"])[1]
        == "gpt"
    )


def test_router_rejects_role_binding_with_unknown_provider():
    with pytest.raises(ValueError, match="providers ausentes"):
        _router(
            {"judge": {ModelTier.STANDARD: TierBinding(provider_name="x", model="m")}}
        )


@pytest.mark.asyncio
async def test_complete_honours_role_and_avoid_models_from_request():
    router = _router(
        {
            "judge": {
                ModelTier.STANDARD: TierBinding(provider_name="openai", model="gpt")
            }
        }
    )
    plain = await router.complete(ModelTier.STANDARD, _req())
    judge = await router.complete(ModelTier.STANDARD, _req(role="judge"))
    escalated = await router.complete(ModelTier.STANDARD, _req(avoid_models=["sonnet"]))
    assert (plain.model, judge.model, escalated.model) == ("sonnet", "gpt", "opus")


def test_settings_parse_judge_bindings():
    settings = Settings(
        judge_tier_bindings_json='{"2": {"provider_name": "anthropic", "model": "claude-opus-5"}}'
    )
    assert settings.judge_tier_bindings == {
        ModelTier.STANDARD: TierBinding(
            provider_name="anthropic", model="claude-opus-5"
        )
    }
    assert Settings().judge_tier_bindings == {}
    assert Settings().judge_independence == "bindings"
    assert Settings().judge_critical_quorum == 2


# --------------------------------------------------------------------------
# Judge
# --------------------------------------------------------------------------


class ScriptedRouter:
    """Responde vereditos em sequência e devolve como `model` o que o request
    pediu para evitar (simula um router que trocou de modelo) ou "sonnet"."""

    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.requests: list[CompletionRequest] = []

    async def complete(self, tier, request):
        self.requests.append(request)
        model = "opus" if "sonnet" in request.avoid_models else "sonnet"
        if len(request.avoid_models) >= 2:
            model = "haiku"
        return CompletionResult(
            text="ok",
            parsed=self._verdicts.pop(0),
            model=model,
            provider="fake",
            usage=Usage(input_tokens=5, output_tokens=0),
            cost_usd=0.01,
            latency_ms=0.0,
        )


def _verdict(score: float, *, failures=(), required=()):
    return {
        "criteria": [
            {"index": 1, "criterion": "clareza", "score": score, "reasoning": "r"}
        ],
        "failures": list(failures),
        "required_changes": list(required),
        "overall_score": score,
        "approved": score >= 0.7,
    }


def _task(*, critical=False, executor_model="sonnet") -> AgentTask:
    now = datetime.now(timezone.utc)
    return AgentTask(
        title="t",
        description="d",
        capability=Capability.BACKEND,
        acceptance_criteria=["clareza"],
        is_critical=critical,
        result={"summary": "s"},
        attempts=[
            TaskAttempt(
                attempt_number=1,
                agent_name="backend_executor",
                model=executor_model,
                started_at=now,
                finished_at=now,
                outcome=TaskStatus.RUNNING,
            )
        ],
        status=TaskStatus.RUNNING,
    )


@pytest.mark.asyncio
async def test_bindings_mode_records_dependence_without_escalating():
    router = ScriptedRouter([_verdict(0.9)])
    outcome = await LLMJudge(router, independence="bindings").evaluate(_task(), {})
    request = router.requests[0]
    assert request.role == "judge"
    assert request.avoid_models == []
    assert outcome.evaluation.judge_models == ["sonnet"]
    assert outcome.evaluation.independent_judge is False  # mesmo modelo do executor
    assert outcome.evaluation.approved is True


@pytest.mark.asyncio
async def test_escalate_mode_asks_router_to_avoid_executor_model():
    router = ScriptedRouter([_verdict(0.9)])
    outcome = await LLMJudge(router, independence="escalate").evaluate(_task(), {})
    assert router.requests[0].avoid_models == ["sonnet"]
    assert outcome.evaluation.judge_models == ["opus"]
    assert outcome.evaluation.independent_judge is True


@pytest.mark.asyncio
async def test_off_mode_does_not_record_independence():
    router = ScriptedRouter([_verdict(0.9)])
    outcome = await LLMJudge(router, independence="off").evaluate(_task(), {})
    assert router.requests[0].avoid_models == []
    assert outcome.evaluation.independent_judge is None
    assert outcome.evaluation.judge_models == ["sonnet"]


@pytest.mark.asyncio
async def test_unknown_executor_model_yields_no_verdict_on_independence():
    router = ScriptedRouter([_verdict(0.9)])
    outcome = await LLMJudge(router).evaluate(_task(executor_model="unknown"), {})
    assert outcome.evaluation.independent_judge is None


@pytest.mark.asyncio
async def test_critical_task_requires_unanimous_quorum():
    router = ScriptedRouter(
        [
            _verdict(0.9),
            _verdict(0.3, failures=["falta validação"], required=["valide a entrada"]),
        ]
    )
    judge = LLMJudge(router, independence="escalate", critical_quorum=2)
    outcome = await judge.evaluate(_task(critical=True), {})

    assert len(router.requests) == 2
    assert router.requests[0].avoid_models == ["sonnet"]
    assert router.requests[1].avoid_models == ["opus", "sonnet"], (
        "segundo juiz evita o primeiro"
    )
    ev = outcome.evaluation
    assert ev.judge_models == ["opus", "haiku"]
    assert ev.independent_judge is True
    assert ev.approved is False
    assert ev.criteria_scores == {"clareza": 0.3}, "menor nota entre os juízes"
    assert ev.failures[0] == "[judge haiku] falta validação"
    assert any(f.startswith("[quorum] juízes divergiram: 1 de 2") for f in ev.failures)
    assert ev.required_changes == ["valide a entrada"]
    assert outcome.usage.tokens == 10


@pytest.mark.asyncio
async def test_critical_task_with_unanimous_approval_passes():
    router = ScriptedRouter([_verdict(0.9), _verdict(0.8)])
    outcome = await LLMJudge(router, critical_quorum=2).evaluate(
        _task(critical=True), {}
    )
    assert len(router.requests) == 2
    assert outcome.evaluation.approved is True
    assert outcome.evaluation.criteria_scores == {"clareza": 0.8}
    assert outcome.evaluation.failures == []


@pytest.mark.asyncio
async def test_non_critical_task_gets_single_verdict_even_with_quorum():
    router = ScriptedRouter([_verdict(0.9)])
    outcome = await LLMJudge(router, critical_quorum=3).evaluate(_task(), {})
    assert len(router.requests) == 1
    assert outcome.evaluation.judge_models == ["sonnet"]


@pytest.mark.asyncio
async def test_quorum_one_disables_second_verdict_on_critical_task():
    router = ScriptedRouter([_verdict(0.9)])
    outcome = await LLMJudge(router, critical_quorum=1).evaluate(
        _task(critical=True), {}
    )
    assert len(router.requests) == 1
    assert outcome.evaluation.approved is True


def test_pricing_untouched_by_role_bindings():
    # sanidade: ModelPricing continua a única fonte de custo, independente do papel
    assert (
        ModelPricing(input_per_mtok=1, output_per_mtok=2).cost(
            Usage(input_tokens=1_000_000)
        )
        == 1
    )
