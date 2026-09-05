"""Budget stops must retain applied edits and their attempt evidence."""

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agents.executor import LLMExecutor
from app.graph.contracts import NodeDependencies
from app.graph.state import WorkflowBudget
from app.graph.workflow import build_serde, build_workflow
from app.infrastructure.scm import collect_publishable_changes
from app.infrastructure.workspace_runtime import (
    CommandObjectiveValidator,
    LocalWorkspaceRuntime,
)
from app.models.task import AgentTask, Capability, EvaluationResult, TaskStatus
from app.providers.base import (
    CompletionRequest,
    CompletionResult,
    LLMProvider,
    Message,
    Usage,
)
from app.providers.registry import ModelTier, ProviderRouter, TierBinding


class Provider(LLMProvider):
    name = "fixture"

    def __init__(self):
        super().__init__({}, max_retries=0)
        self.calls = 0

    @staticmethod
    def estimate_request_usage(request):
        # This deterministic provider consumes exactly this amount per call.
        return Usage(input_tokens=1000, output_tokens=100)

    async def _do_complete(self, request):
        self.calls += 1
        operations = (
            [
                {
                    "op": "replace",
                    "path": "orders.py",
                    "search": "broken",
                    "replace": "fixed",
                }
            ]
            if self.calls == 1
            else [
                {"op": "create", "path": "test_orders.py", "content": "# regression\n"}
            ]
        )
        return CompletionResult(
            text="",
            parsed={"summary": "fix", "operations": operations},
            model=request.model,
            provider=self.name,
            usage=Usage(input_tokens=1000, output_tokens=100),
            cost_usd=0.001,
            latency_ms=0,
        )


class Planner:
    async def create_plan(self, request, context):
        return [
            AgentTask(
                title="repair",
                description="repair and test",
                capability=Capability.BACKEND,
                acceptance_criteria=["fixed"],
            )
        ]


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
    def __init__(self):
        self.calls = 0

    async def evaluate(self, task, context):
        self.calls += 1
        return EvaluationResult(
            task_id=task.id, approved=True, score=1, criteria_scores={"fixed": 1}
        )


class Runner:
    def __init__(self, fail_first):
        self.fail_first = fail_first
        self.calls = 0

    async def run(self, command, root, output_limit):
        self.calls += 1
        return {
            "exit_code": int(self.fail_first and self.calls == 1),
            "stdout": "",
            "stderr": "missing regression" if self.calls == 1 else "",
        }


def application(root, *, fail_first=False, judge=None, provider=None, tools=None):
    provider = provider or Provider()
    router = ProviderRouter(
        {"fixture": provider},
        {ModelTier.STANDARD: TierBinding(provider_name="fixture", model="executor")},
    )
    validator = CommandObjectiveValidator(
        name="pytest",
        command="pytest",
        workspace_root=str(root),
        command_runner=Runner(fail_first),
    )
    executor = LLMExecutor(
        router,
        "executor",
        max_autocorrect_rounds=1,
        tools=tools,
        workspace_runtime=LocalWorkspaceRuntime(
            str(root), apply_files_enabled=True, command_feedback_runners=[validator]
        ),
    )
    selected_judge = judge(provider) if judge else Judge()
    graph = build_workflow(
        Planner(),
        Registry(executor),
        selected_judge,
        Memory(),
        MemorySaver(serde=build_serde()),
    )
    return graph, provider, selected_judge


def initial(tokens):
    return {
        "request": "repair",
        "project_id": "fixture",
        "workflow_id": "retry-budget",
        "owner_client_id": "test",
        "budget": WorkflowBudget(max_tokens=tokens),
    }


@pytest.mark.asyncio
async def test_inner_budget_stop_preserves_applied_result_for_authorized_retry(
    tmp_path,
):
    (tmp_path / "orders.py").write_text("broken\n")
    graph, provider, judge = application(tmp_path, fail_first=True)
    config = {"configurable": {"thread_id": "inner-budget"}}
    stopped = await graph.ainvoke(initial(1500), config)
    assert (tmp_path / "orders.py").read_text() == "fixed\n"
    task = stopped["plan"][0]
    assert task.result is not None, (
        "applied first round must survive a refused correction"
    )
    assert task.result["workspace"]["published_files"] == [
        {"path": "orders.py", "content": "fixed\n"}
    ]
    assert task.result["workspace"]["autocorrect"]["stopped_reason"] == "budget_blocked"
    assert task.status == TaskStatus.ESCALATED
    assert judge.calls == 0
    assert provider.calls == 1
    assert stopped["usage"]["tokens"] == 1100
    assert task.attempts[-1].tokens_used == 1100
    assert stopped["__interrupt__"][0].value["reason"] == "budget_exhausted"

    resumed = await graph.ainvoke(Command(resume="retry"), config)
    task = resumed["plan"][0]
    files, deleted = collect_publishable_changes([task.model_dump(mode="json")])
    assert {item["path"]: item["content"] for item in files} == {
        "orders.py": "fixed\n",
        "test_orders.py": "# regression\n",
    }
    assert deleted == []
    assert task.status == TaskStatus.COMPLETED
    assert task.attempt_count == 2
    assert judge.calls == 1


@pytest.mark.asyncio
async def test_interrupted_tool_round_keeps_all_executor_usage_and_previous_artifact(
    tmp_path,
):
    from app.agents.tools import ReadFileTool
    from app.providers.base import ToolCall

    class ExploringProvider(Provider):
        async def _do_complete(self, request):
            result = await super()._do_complete(request)
            if self.calls == 2:
                return result.model_copy(
                    update={
                        "parsed": None,
                        "tool_calls": [
                            ToolCall(
                                id="read-current",
                                name="read_file",
                                arguments={"path": "orders.py"},
                            )
                        ],
                    }
                )
            return result

    (tmp_path / "orders.py").write_text("broken\n")
    graph, provider, judge = application(
        tmp_path,
        fail_first=True,
        provider=ExploringProvider(),
        tools=[ReadFileTool(str(tmp_path))],
    )
    stopped = await graph.ainvoke(
        initial(2600), {"configurable": {"thread_id": "tool-budget"}}
    )
    task = stopped["plan"][0]
    assert task.result["workspace"]["published_files"] == [
        {"path": "orders.py", "content": "fixed\n"}
    ]
    assert task.result["workspace"]["autocorrect"]["total_iterations"] == 1
    assert task.result["workspace"]["autocorrect"]["stopped_reason"] == "budget_blocked"
    assert task.status == TaskStatus.ESCALATED
    assert provider.calls == 2
    assert judge.calls == 0
    assert task.attempts[-1].model == "executor"
    assert task.attempts[-1].tokens_used == 2200
    assert task.attempts[-1].cost_usd == 0.002
    assert stopped["usage"]["tokens"] == 2200
    assert stopped["usage"]["cost_usd"] == 0.002
    assert stopped["__interrupt__"][0].value["reason"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_judge_budget_stop_retains_current_result_and_executor_only_attempt_usage(
    tmp_path,
):
    class BudgetJudge:
        def __init__(self, provider):
            self.provider = provider

        async def evaluate(self, task, context):
            request = CompletionRequest(
                model="judge", messages=[Message(role="user", content="review")]
            )
            await self.provider.complete(request)
            await self.provider.complete(request)
            raise AssertionError("the second judge call must be refused")

    (tmp_path / "orders.py").write_text("broken\n")
    graph, provider, _ = application(tmp_path, judge=BudgetJudge)
    stopped = await graph.ainvoke(
        initial(2600), {"configurable": {"thread_id": "judge-budget"}}
    )
    task = stopped["plan"][0]
    assert task.result["workspace"]["published_files"] == [
        {"path": "orders.py", "content": "fixed\n"}
    ]
    assert task.status == TaskStatus.ESCALATED
    assert provider.calls == 2
    assert task.attempts[-1].tokens_used == 1100
    assert task.attempts[-1].cost_usd == 0.001
    assert task.budget.consumed_tokens == 2200
    assert stopped["usage"]["tokens"] == 2200
    assert stopped["usage"]["cost_usd"] == 0.002
    assert stopped["__interrupt__"][0].value["reason"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_error_after_execution_keeps_new_result_instead_of_original_task(
    tmp_path, monkeypatch
):
    def unavailable_judge(self, workspace):
        raise RuntimeError("judge runtime unavailable")

    monkeypatch.setattr(NodeDependencies, "active_judge", unavailable_judge)
    (tmp_path / "orders.py").write_text("broken\n")
    graph, provider, _ = application(tmp_path)
    stopped = await graph.ainvoke(
        initial(1500), {"configurable": {"thread_id": "judge-runtime"}}
    )
    task = stopped["plan"][0]
    assert task.result is not None, (
        "post-execution failure must retain the newly applied result"
    )
    assert task.result["workspace"]["published_files"] == [
        {"path": "orders.py", "content": "fixed\n"}
    ]
    assert task.attempts[0].model == "executor"
    assert "judge runtime unavailable" in task.attempts[0].failure_reason
    assert stopped["usage"]["tokens"] == 1100
    assert task.status == TaskStatus.ESCALATED


@pytest.mark.asyncio
async def test_budget_failure_after_build_retains_fresh_report_and_attempt(
    tmp_path, monkeypatch
):
    from app.graph.contracts import ExecutionPayload
    from app.graph.nodes import build_nodes
    from app.infrastructure.llm_budget import BudgetAdmissionError
    from app.models.factory import BuildProfileSelection, FactoryStage
    from tests.unit.test_factory_graph import BuildRunner, lease

    def unavailable_judge(self, workspace):
        raise BudgetAdmissionError("judge setup cannot reserve the next call")

    monkeypatch.setattr(NodeDependencies, "active_judge", unavailable_judge)
    (tmp_path / "orders.py").write_text("broken\n")
    provider = Provider()
    router = ProviderRouter(
        {"fixture": provider},
        {ModelTier.STANDARD: TierBinding(provider_name="fixture", model="executor")},
    )
    executor = LLMExecutor(
        router,
        "executor",
        workspace_runtime=LocalWorkspaceRuntime(
            str(tmp_path), apply_files_enabled=True
        ),
    )
    task = (await Planner().create_plan("fix", {}))[0]
    nodes = build_nodes(
        Planner(), Registry(executor), Judge(), Memory(), build_runner=BuildRunner()
    )
    update = await nodes["execute_task"](
        ExecutionPayload(
            task=task,
            project_id="fixture",
            context={},
            workspace=lease(tmp_path, "budget-build"),
            factory_stage=FactoryStage.IMPLEMENTATION,
            build_strategy=BuildProfileSelection(
                selected_profile="python-tests", selection_reason="explicit"
            ),
            token_allowance=2000,
            cost_allowance_usd=1,
        )
    )
    changed = update["plan"][0]
    assert changed.status == TaskStatus.ESCALATED
    assert changed.result["workspace"]["published_files"] == [
        {"path": "orders.py", "content": "fixed\n"}
    ]
    attempt = changed.attempts[-1]
    assert attempt.build_validation.outcome.value == "success"
    assert attempt.factory_stage == FactoryStage.VALIDATION
    assert attempt.model == "executor"
    assert attempt.tokens_used == 1100
    assert attempt.cost_usd == 0.001
    assert "judge setup" in attempt.failure_reason
    assert update["budget_blocked_reason"]
    assert update["usage"]["tokens"] == 1100
    assert update["factory_stage"] == FactoryStage.VALIDATION
