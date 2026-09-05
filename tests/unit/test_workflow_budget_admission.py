"""Budget admission exercises the real graph and provider boundary without network."""

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.graph.contracts import PlanningOutcome, UsageReport
from app.graph.state import WorkflowBudget
from app.graph.workflow import build_serde, build_workflow
from app.models.task import AgentTask, Capability, EvaluationResult
from app.providers.base import (
    CompletionRequest,
    CompletionResult,
    LLMProvider,
    Message,
    Usage,
)
from app.providers.registry import ModelTier, ProviderRouter, TierBinding


class MeasuredProvider(LLMProvider):
    name = "measured"

    def __init__(self):
        super().__init__({}, max_retries=0)
        self.calls = []

    async def _do_complete(self, request):
        self.calls.append(request)
        tokens = len(request.messages[0].content)
        return CompletionResult(
            text="ok",
            model=request.model,
            provider=self.name,
            usage=Usage(input_tokens=tokens),
            cost_usd=0.001,
            latency_ms=0,
        )


def request(tokens):
    return CompletionRequest(
        model="", messages=[Message(role="user", content="a" * tokens)], max_tokens=64
    )


class Memory:
    async def load_context(self, project_id, request=""):
        return {}

    async def persist(self, state):
        pass


class Registry:
    def __init__(self, executor):
        self.executor = executor

    def select(self, task):
        return self.executor


class Judge:
    async def evaluate(self, task, context):
        return EvaluationResult(
            task_id=task.id, approved=True, score=1, criteria_scores={"ok": 1}
        )


@pytest.mark.asyncio
async def test_workflow_refuses_call_whose_input_would_exceed_remaining_tokens():
    provider = MeasuredProvider()
    router = ProviderRouter(
        {"measured": provider},
        {ModelTier.STANDARD: TierBinding(provider_name="measured", model="fake")},
    )

    class Planner:
        async def create_plan(self, text, context):
            result = await router.complete(None, request(60_000))
            return PlanningOutcome(
                plan=[
                    AgentTask(
                        title="fix",
                        description="fix",
                        capability=Capability.BACKEND,
                        acceptance_criteria=["ok"],
                    )
                ],
                usage=UsageReport(
                    tokens=result.usage.total_tokens, cost_usd=result.cost_usd
                ),
            )

    class Executor:
        async def execute(self, task, context):
            result = await router.complete(None, request(45_000))
            return {
                "result": {"ok": True},
                "tokens": result.usage.total_tokens,
                "cost_usd": result.cost_usd,
            }

    app = build_workflow(
        Planner(),
        Registry(Executor()),
        Judge(),
        Memory(),
        MemorySaver(serde=build_serde()),
    )
    output = await app.ainvoke(
        {
            "request": "fix",
            "project_id": "fixture",
            "workflow_id": "bounded",
            "owner_client_id": "test",
            "budget": WorkflowBudget(max_tokens=100_000),
        },
        {"configurable": {"thread_id": "bounded"}},
    )

    assert len(provider.calls) == 1, (
        "executor input must be reserved before calling the provider"
    )
    assert output["usage"]["tokens"] == 60_000
    assert output["__interrupt__"][0].value["reason"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_judge_consumes_same_allowance_and_partial_executor_usage_survives_failure():
    from app.graph.nodes import build_nodes
    from app.graph.state import WorkflowState

    provider = MeasuredProvider()
    router = ProviderRouter(
        {"measured": provider},
        {ModelTier.STANDARD: TierBinding(provider_name="measured", model="fake")},
    )

    class Executor:
        async def execute(self, task, context):
            result = await router.complete(None, request(10_000))
            return {
                "result": {"ok": True},
                "tokens": result.usage.total_tokens,
                "cost_usd": result.cost_usd,
            }

    class CallingJudge:
        async def evaluate(self, task, context):
            await router.complete(None, request(15_000))
            return await Judge().evaluate(task, context)

    task = AgentTask(
        title="fix",
        description="fix",
        capability=Capability.BACKEND,
        acceptance_criteria=["ok"],
    )
    state = WorkflowState(
        request="fix",
        project_id="fixture",
        workflow_id="budget-judge",
        owner_client_id="test",
        plan=[task],
        budget=WorkflowBudget(max_tokens=25_000),
    )
    nodes = build_nodes(None, Registry(Executor()), CallingJudge(), Memory())
    payload = nodes["route_to_execution"](state)[0].arg
    update = await nodes["execute_task"](payload)
    assert len(provider.calls) == 1
    assert update["usage"]["tokens"] == 10_000
    assert update["usage"]["cost_usd"] == 0.001
    assert update["budget_blocked_reason"].startswith("llm_budget_admission")
    assert update["plan"][0].status.value == "escalated"


@pytest.mark.asyncio
async def test_fanout_partitions_remaining_global_allowance():
    import asyncio
    from app.graph.nodes import build_nodes
    from app.graph.state import WorkflowState

    provider = MeasuredProvider()
    router = ProviderRouter(
        {"measured": provider},
        {ModelTier.STANDARD: TierBinding(provider_name="measured", model="fake")},
    )

    class Executor:
        async def execute(self, task, context):
            result = await router.complete(None, request(20_000))
            return {
                "result": {"ok": True},
                "tokens": result.usage.total_tokens,
                "cost_usd": result.cost_usd,
            }

    tasks = [
        AgentTask(
            title=f"fix-{index}",
            description="fix",
            capability=Capability.BACKEND,
            acceptance_criteria=["ok"],
        )
        for index in range(2)
    ]
    state = WorkflowState(
        request="fix",
        project_id="fixture",
        workflow_id="fanout-budget",
        owner_client_id="test",
        plan=tasks,
        budget=WorkflowBudget(max_tokens=40_000),
        usage={"tokens": 10_000, "cost_usd": 1},
    )
    nodes = build_nodes(None, Registry(Executor()), Judge(), Memory())
    sends = nodes["route_to_execution"](state)
    assert [send.arg.token_allowance for send in sends] == [15_000, 15_000]
    assert sum(send.arg.cost_allowance_usd for send in sends) == 4
    updates = await asyncio.gather(*(nodes["execute_task"](send.arg) for send in sends))
    assert not provider.calls
    assert all(update["budget_blocked_reason"] for update in updates)


@pytest.mark.asyncio
async def test_provider_retry_cannot_reuse_allowance_with_unknown_consumption():
    from app.infrastructure.llm_budget import (
        BudgetAdmissionError,
        CallBudget,
        call_budget_scope,
    )
    from app.providers.base import RetryableProviderError

    class RetryProvider(MeasuredProvider):
        def __init__(self):
            super().__init__()
            self._max_retries = 2
            self._base_backoff = 0

        async def _do_complete(self, request):
            self.calls.append(request)
            raise RetryableProviderError(
                "connection lost after send", provider=self.name
            )

    provider = RetryProvider()
    req = request(10_000).model_copy(update={"model": "fake"})
    estimate = provider.estimate_request_usage(req)
    budget = CallBudget(max_tokens=estimate.total_tokens, max_cost_usd=5)
    with call_budget_scope(budget), pytest.raises(BudgetAdmissionError):
        await provider.complete(req)
    assert len(provider.calls) == 1
    assert budget.tokens == 0
    assert budget.unconfirmed_tokens == estimate.total_tokens
    assert budget.unconfirmed_cost_usd > 0


@pytest.mark.asyncio
async def test_budget_scope_isolated_between_parallel_workflows():
    import asyncio
    from app.infrastructure.llm_budget import (
        BudgetAdmissionError,
        CallBudget,
        call_budget_scope,
    )

    provider = MeasuredProvider()
    req = request(10_000).model_copy(update={"model": "fake"})

    async def run(tokens):
        budget = CallBudget(max_tokens=tokens, max_cost_usd=5)
        with call_budget_scope(budget):
            try:
                await provider.complete(req)
            except BudgetAdmissionError:
                pass
        return budget

    blocked, allowed = await asyncio.gather(run(100), run(20_000))
    assert blocked.tokens == 0
    assert blocked.blocked_reason
    assert allowed.tokens == 10_000
    assert not allowed.blocked_reason


def test_estimate_includes_tool_definitions_arguments_results_and_multibyte_text():
    from app.providers.base import ToolCall, ToolResult, ToolSpec

    req = request(10)
    simple = LLMProvider.estimate_request_usage(req).total_tokens
    tool_request = req.model_copy(
        update={
            "tools": [ToolSpec(name="read", description="d" * 1000)],
            "messages": [
                Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(id="1", name="read", arguments={"path": "p" * 1000})
                    ],
                ),
                Message(
                    role="user",
                    tool_results=[
                        ToolResult(tool_call_id="1", name="read", content="á" * 1000)
                    ],
                ),
            ],
        }
    )
    assert LLMProvider.estimate_request_usage(tool_request).total_tokens > simple + 4000


@pytest.mark.asyncio
async def test_tool_loop_stops_before_sending_growing_tool_results(tmp_path):
    from app.agents.tools import ReadFileTool, ToolLoop
    from app.infrastructure.llm_budget import (
        BudgetAdmissionError,
        CallBudget,
        call_budget_scope,
    )
    from app.providers.base import ToolCall

    (tmp_path / "large.py").write_text("x" * 15_000)

    class ExploringProvider(MeasuredProvider):
        async def _do_complete(self, request):
            result = await super()._do_complete(request)
            return result.model_copy(
                update={
                    "tool_calls": [
                        ToolCall(
                            id="read-1",
                            name="read_file",
                            arguments={"path": "large.py"},
                        )
                    ],
                    "usage": Usage(input_tokens=1000),
                }
            )

    provider = ExploringProvider()
    router = ProviderRouter(
        {"measured": provider},
        {ModelTier.STANDARD: TierBinding(provider_name="measured", model="fake")},
    )
    loop = ToolLoop(router, [ReadFileTool(str(tmp_path))])
    budget = CallBudget(max_tokens=15_000, max_cost_usd=5)
    with call_budget_scope(budget), pytest.raises(BudgetAdmissionError):
        await loop.run(None, request(100), token_ceiling=15_000)
    assert len(provider.calls) == 1
    assert budget.tokens == 1000


@pytest.mark.asyncio
async def test_partial_execution_usage_is_persisted_when_later_call_is_blocked():
    from app.graph.nodes import build_nodes
    from app.graph.state import WorkflowState

    provider = MeasuredProvider()
    router = ProviderRouter(
        {"measured": provider},
        {ModelTier.STANDARD: TierBinding(provider_name="measured", model="fake")},
    )

    class Executor:
        async def execute(self, task, context):
            await router.complete(None, request(10_000))
            await router.complete(None, request(20_000))
            raise AssertionError("must not admit second call")

    task = AgentTask(
        title="fix",
        description="fix",
        capability=Capability.BACKEND,
        acceptance_criteria=["ok"],
    )
    state = WorkflowState(
        request="fix",
        project_id="fixture",
        workflow_id="partial-budget",
        owner_client_id="test",
        plan=[task],
        budget=WorkflowBudget(max_tokens=25_000),
    )
    nodes = build_nodes(None, Registry(Executor()), Judge(), Memory())
    update = await nodes["execute_task"](nodes["route_to_execution"](state)[0].arg)
    assert len(provider.calls) == 1
    assert update["usage"]["tokens"] == 10_000
    assert update["plan"][0].budget.consumed_tokens == 10_000
    assert update["plan"][0].attempts[-1].tokens_used == 10_000


@pytest.mark.asyncio
async def test_task_budget_limits_provider_even_with_large_workflow_budget():
    from app.graph.nodes import build_nodes
    from app.graph.state import WorkflowState
    from app.models.task import TaskBudget

    provider = MeasuredProvider()
    router = ProviderRouter(
        {"measured": provider},
        {ModelTier.STANDARD: TierBinding(provider_name="measured", model="fake")},
    )

    class Executor:
        async def execute(self, task, context):
            await router.complete(None, request(10_000))
            raise AssertionError("task allowance must refuse the call")

    task = AgentTask(
        title="fix",
        description="fix",
        capability=Capability.BACKEND,
        acceptance_criteria=["ok"],
        budget=TaskBudget(
            max_tokens=12_000, consumed_tokens=1000, unconfirmed_tokens=1000
        ),
    )
    state = WorkflowState(
        request="fix",
        project_id="fixture",
        workflow_id="task-budget",
        owner_client_id="test",
        plan=[task],
    )
    nodes = build_nodes(None, Registry(Executor()), Judge(), Memory())
    update = await nodes["execute_task"](nodes["route_to_execution"](state)[0].arg)
    assert not provider.calls
    assert update["plan"][0].status.value == "escalated"
    assert update["plan"][0].budget.unconfirmed_tokens == 1000


@pytest.mark.asyncio
async def test_planner_failure_persists_metered_usage_and_requires_decision():
    provider = MeasuredProvider()
    router = ProviderRouter(
        {"measured": provider},
        {ModelTier.STANDARD: TierBinding(provider_name="measured", model="fake")},
    )

    class Planner:
        async def create_plan(self, text, context):
            await router.complete(None, request(1000))
            raise ValueError("invalid dependency graph")

    app = build_workflow(
        Planner(), Registry(None), Judge(), Memory(), MemorySaver(serde=build_serde())
    )
    output = await app.ainvoke(
        {
            "request": "fix",
            "project_id": "fixture",
            "workflow_id": "planner-failure",
            "owner_client_id": "test",
        },
        {"configurable": {"thread_id": "planner-failure"}},
    )
    assert output["usage"]["tokens"] == 1000
    assert output["__interrupt__"][0].value["reason"] == "llm_call_failed"


@pytest.mark.asyncio
async def test_authorized_retry_adds_headroom_and_restarts_blocked_planning():
    from langgraph.types import Command

    provider = MeasuredProvider()
    router = ProviderRouter(
        {"measured": provider},
        {ModelTier.STANDARD: TierBinding(provider_name="measured", model="fake")},
    )

    class Planner:
        async def create_plan(self, text, context):
            await router.complete(None, request(15_000))
            return [
                AgentTask(
                    title="fix",
                    description="fix",
                    capability=Capability.BACKEND,
                    acceptance_criteria=["ok"],
                )
            ]

    class Executor:
        async def execute(self, task, context):
            return {"result": {"ok": True}}

    app = build_workflow(
        Planner(),
        Registry(Executor()),
        Judge(),
        Memory(),
        MemorySaver(serde=build_serde()),
    )
    config = {"configurable": {"thread_id": "planner-retry"}}
    stopped = await app.ainvoke(
        {
            "request": "fix",
            "project_id": "fixture",
            "workflow_id": "planner-retry",
            "owner_client_id": "test",
            "budget": WorkflowBudget(max_tokens=10_000),
        },
        config,
    )
    assert not provider.calls
    assert stopped["__interrupt__"][0].value["reason"] == "budget_exhausted"
    assert stopped["error"].startswith("llm_budget_admission")
    resumed = await app.ainvoke(Command(resume="retry"), config)
    assert len(provider.calls) == 1
    assert resumed["budget"].max_tokens == 20_000
    assert resumed["budget_blocked_reason"] is None
    assert resumed["error"] is None
    assert resumed["usage"]["tokens"] == 15_000
    assert resumed["phase"].value == "completed"


def test_dispatch_concurrency_limit_does_not_reserve_allowance_for_unsent_tasks():
    from app.graph.nodes import build_nodes
    from app.graph.state import WorkflowState

    class LimitedRegistry(Registry):
        def dispatch_policy(self, task):
            return "backend", 1

    tasks = [
        AgentTask(
            title=f"fix-{index}",
            description="fix",
            capability=Capability.BACKEND,
            acceptance_criteria=["ok"],
        )
        for index in range(10)
    ]
    state = WorkflowState(
        request="fix",
        project_id="fixture",
        workflow_id="limited-fanout",
        owner_client_id="test",
        plan=tasks,
        budget=WorkflowBudget(max_tokens=100_000, max_cost_usd=0.5),
    )
    nodes = build_nodes(None, LimitedRegistry(None), Judge(), Memory())
    sends = nodes["route_to_execution"](state)
    assert len(sends) == 1
    assert sends[0].arg.token_allowance == 100_000
    assert sends[0].arg.cost_allowance_usd == 0.5


@pytest.mark.asyncio
async def test_legacy_judging_reserves_and_charges_task_as_well_as_workflow():
    from app.graph.nodes import build_nodes
    from app.graph.state import WorkflowState
    from app.models.task import TaskBudget, TaskStatus

    provider = MeasuredProvider()
    router = ProviderRouter(
        {"measured": provider},
        {ModelTier.STANDARD: TierBinding(provider_name="measured", model="fake")},
    )

    class CallingJudge:
        async def evaluate(self, task, context):
            await router.complete(None, request(10_000))
            return await Judge().evaluate(task, context)

    blocked = AgentTask(
        title="small",
        description="fix",
        capability=Capability.BACKEND,
        acceptance_criteria=["ok"],
        status=TaskStatus.RUNNING,
        budget=TaskBudget(max_tokens=1000),
    )
    state = WorkflowState(
        request="fix",
        project_id="fixture",
        workflow_id="legacy-budget",
        owner_client_id="test",
        plan=[blocked],
    )
    nodes = build_nodes(None, Registry(None), CallingJudge(), Memory())
    update = await nodes["evaluate_results"](state)
    assert not provider.calls
    assert update["plan"][0].status == TaskStatus.ESCALATED
    assert update["budget_blocked_reason"]

    allowed = blocked.model_copy(update={"budget": TaskBudget(max_tokens=20_000)})
    update = await nodes["evaluate_results"](
        state.model_copy(update={"plan": [allowed]})
    )
    assert len(provider.calls) == 1
    assert update["usage"]["tokens"] == 10_000
    assert update["plan"][0].budget.consumed_tokens == 10_000
