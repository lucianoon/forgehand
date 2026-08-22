from datetime import datetime, timezone

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agents.advisor import LLMAdvisor
from app.agents.registry import CapabilityExecutorRegistry
from app.models.contracts import AdvisingOutcome, UsageReport
from app.graph.workflow import build_serde, build_workflow
from app.graph.state import WorkflowBudget
from app.models.task import (
    AdvisorTrigger,
    AgentTask,
    Capability,
    EvaluationResult,
    TaskAttempt,
    TaskStatus,
)
from app.providers.base import CompletionResult, Usage
from app.providers.registry import ModelTier


class Memory:
    async def load_context(self, project_id, request=""):
        return {}

    async def persist(self, state):
        return None


class Executor:
    def __init__(self, tokens=1, cost=0.0):
        self.tokens = tokens
        self.cost = cost
        self.calls = 0

    async def execute(self, task, context):
        self.calls += 1
        return {
            "result": {"ok": True},
            "agent": "fake",
            "model": "fake",
            "tokens": self.tokens,
            "cost_usd": self.cost,
        }


class RecordingRegistry:
    def __init__(self, executor):
        self.executor = executor
        self.selected = []

    def select(self, task):
        self.selected.append(task)
        return self.executor


class RejectingJudge:
    async def evaluate(self, task, context):
        return EvaluationResult(
            task_id=task.id,
            approved=False,
            score=0,
            criteria_scores={"ok": 0},
            required_changes=["corrigir"],
        )


class RecordingAdvisor:
    def __init__(self, escalate=True):
        self.calls = []
        self.escalate = escalate

    async def advise(self, trigger, evaluations, context):
        self.calls.append(trigger)
        return AdvisingOutcome(
            diagnosis="causa raiz identificada",
            guidance=["mude a abordagem"],
            escalate_tier=self.escalate,
            usage=UsageReport(tokens=7, cost_usd=0.001),
        )


class FailingAdvisor:
    async def advise(self, trigger, evaluations, context):
        raise RuntimeError("advisor indisponível")


def initial(workflow_id, budget=None):
    data = {
        "request": "teste",
        "project_id": "p",
        "workflow_id": workflow_id,
        "owner_client_id": "client-p",
    }
    if budget is not None:
        data["budget"] = budget
    return data


def make_task(title="t", dependencies=None):
    return AgentTask(
        title=title,
        description="descrição original",
        capability=Capability.BACKEND,
        acceptance_criteria=["ok"],
        dependencies=dependencies or [],
    )


# --------------------------------------------------------------------------
# Agente LLMAdvisor
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_advisor_uses_strong_tier_and_parses_output():
    class FakeRouter:
        def __init__(self):
            self.requests = []
            self.tiers = []

        async def complete(self, tier, request):
            self.tiers.append(tier)
            self.requests.append(request)
            return CompletionResult(
                text="advice",
                parsed={
                    "diagnosis": "abordagem repetida sem tratar o Optional",
                    "guidance": ["Tratar o Optional antes do acesso"],
                    "escalate_tier": True,
                },
                model="fake",
                provider="fake",
                usage=Usage(input_tokens=20, output_tokens=10),
                cost_usd=0.002,
                latency_ms=1,
            )

    task = make_task(title="Corrigir handler").model_copy(
        update={
            "status": TaskStatus.REJECTED,
            "attempts": [
                TaskAttempt(
                    attempt_number=1,
                    agent_name="backend_executor",
                    model="m",
                    started_at=datetime.now(timezone.utc),
                    outcome=TaskStatus.REJECTED,
                    operational_summary={
                        "validation_feedback_text": "mypy: Optional sem narrow"
                    },
                )
            ],
        }
    )
    evaluations = [
        EvaluationResult(
            task_id=task.id,
            approved=False,
            score=0,
            criteria_scores={"ok": 0},
            failures=["mypy falhou"],
            required_changes=["corrigir o tipo"],
        )
    ]

    router = FakeRouter()
    outcome = await LLMAdvisor(router).advise(
        AdvisorTrigger(task=task, judge_rejections=1), evaluations, {}
    )

    assert router.tiers == [ModelTier.STRONG]
    content = router.requests[0].messages[0].content
    assert "mypy: Optional sem narrow" in content
    assert "mypy falhou" in content
    assert "reprovações do judge=1" in content
    assert outcome.escalate_tier is True
    assert outcome.guidance == ["Tratar o Optional antes do acesso"]
    assert outcome.usage.tokens == 30
    assert outcome.usage.cost_usd == pytest.approx(0.002)


# --------------------------------------------------------------------------
# Integração no replan
# --------------------------------------------------------------------------


class OneTaskPlanner:
    async def create_plan(self, request, context):
        return [make_task()]


@pytest.mark.asyncio
async def test_replan_consults_advisor_and_escalates_tier():
    advisor = RecordingAdvisor(escalate=True)
    registry = RecordingRegistry(Executor())
    app = build_workflow(
        OneTaskPlanner(),
        registry,
        RejectingJudge(),
        Memory(),
        MemorySaver(serde=build_serde()),
        advisor=advisor,
    )
    output = await app.ainvoke(
        initial("adv", WorkflowBudget(max_iterations=1)),
        {"configurable": {"thread_id": "adv"}},
    )

    # 1 rejeição → replan consulta o advisor exatamente uma vez
    assert len(advisor.calls) == 1
    assert advisor.calls[0].judge_rejections == 1

    # a segunda execução recebe a orientação e o tier escalado
    assert len(registry.selected) == 2
    retried = registry.selected[1]
    assert retried.tier_escalated is True
    assert "Diagnóstico do advisor" in retried.description
    assert "mude a abordagem" in retried.description
    assert "Correções exigidas pelo judge" in retried.description

    # o consumo do advisor entra no budget do workflow (2 exec + 7 advisor)
    assert output["usage"]["tokens"] == 9


@pytest.mark.asyncio
async def test_replan_skips_advisor_for_tasks_without_attempts():
    class ChainPlanner:
        async def create_plan(self, request, context):
            t1 = make_task(title="t1")
            t2 = make_task(title="t2", dependencies=[t1.id])
            return [t1, t2]

    advisor = RecordingAdvisor()
    app = build_workflow(
        ChainPlanner(),
        RecordingRegistry(Executor()),
        RejectingJudge(),
        Memory(),
        MemorySaver(serde=build_serde()),
        advisor=advisor,
    )
    await app.ainvoke(
        initial("adv-chain", WorkflowBudget(max_iterations=1)),
        {"configurable": {"thread_id": "adv-chain"}},
    )

    # t2 nunca executou (bloqueada em t1): redispatchável, mas sem falha
    # a diagnosticar — o advisor só é consultado para t1.
    assert [t.task.title for t in advisor.calls] == ["t1"]


@pytest.mark.asyncio
async def test_advisor_failure_does_not_block_replan():
    registry = RecordingRegistry(Executor())
    app = build_workflow(
        OneTaskPlanner(),
        registry,
        RejectingJudge(),
        Memory(),
        MemorySaver(serde=build_serde()),
        advisor=FailingAdvisor(),
    )
    await app.ainvoke(
        initial("adv-fail", WorkflowBudget(max_iterations=1)),
        {"configurable": {"thread_id": "adv-fail"}},
    )

    # replan seguiu normalmente só com as correções do judge
    assert len(registry.selected) == 2
    retried = registry.selected[1]
    assert "Correções exigidas pelo judge" in retried.description
    assert "advisor" not in retried.description
    assert retried.tier_escalated is False


# --------------------------------------------------------------------------
# Registry: seleção de executor escalado
# --------------------------------------------------------------------------


def test_registry_selects_escalated_executor_when_task_flagged():
    class FakeRouter:
        def escalate(self, current):
            return ModelTier(min(current + 1, ModelTier.STRONG))

    registry = CapabilityExecutorRegistry(FakeRouter())
    task = make_task()

    assert registry.select(task).tier == ModelTier.STANDARD
    escalated = task.model_copy(update={"tier_escalated": True})
    assert registry.select(escalated).tier == ModelTier.STRONG
