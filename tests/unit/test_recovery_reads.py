"""Recovery source reads retain operator controls and task isolation."""

from datetime import datetime, timezone

import pytest

from app.agents.executor import LLMExecutor
from app.agents.hooks import ToolHookDispatcher, ToolHookRule
from app.agents.tools import ReadFileTool, ToolError, ToolLoop
from app.infrastructure.audit import InMemoryAuditLog
from app.infrastructure.workspace_runtime import LocalWorkspaceRuntime
from app.models.task import AgentTask, Capability, TaskAttempt
from app.providers.base import (
    CompletionRequest,
    CompletionResult,
    Message,
    ToolCall,
    Usage,
)


class Router:
    def __init__(self, payload=None, extra_call=False):
        self.requests = []
        self.payload = payload or {"summary": "done"}
        self.extra_call = extra_call

    async def complete(self, tier, request):
        self.requests.append(request)
        return CompletionResult(
            text="",
            parsed=None if self.extra_call else self.payload,
            tool_calls=[
                ToolCall(id="extra", name="read_file", arguments={"path": "second.cjs"})
            ]
            if self.extra_call
            else [],
            model="test",
            provider="test",
            usage=Usage(input_tokens=10),
            cost_usd=0.01,
            latency_ms=0,
        )


def request():
    return CompletionRequest(
        model="", messages=[Message(role="user", content="repair")]
    )


def read_results(req):
    return [result for message in req.messages for result in message.tool_results]


@pytest.mark.asyncio
async def test_recovery_reads_share_call_budget_and_force_final(tmp_path):
    for name in ("first.cjs", "second.cjs", "third.cjs"):
        (tmp_path / name).write_text(name)
    router = Router()
    loop = ToolLoop(router, [ReadFileTool(str(tmp_path))], max_tool_calls=2)
    outcome = await loop.run(
        None,
        request(),
        refresh_paths=["first.cjs", "first.cjs", "second.cjs", "third.cjs"],
    )
    assert outcome.tool_calls == 2
    assert [trace["arguments"]["path"] for trace in outcome.trace] == [
        "first.cjs",
        "second.cjs",
    ]
    assert router.requests[0].force_final
    assert outcome.stopped_reason == "max_tool_calls"
    assert outcome.tokens == 10 and outcome.cost_usd == 0.01
    calls = router.requests[0].messages[1].tool_calls
    assert [result.tool_call_id for result in read_results(router.requests[0])] == [
        call.id for call in calls
    ]


@pytest.mark.asyncio
async def test_recovery_cannot_execute_an_extra_call_after_limit(tmp_path):
    (tmp_path / "first.cjs").write_text("first")
    loop = ToolLoop(
        Router(extra_call=True), [ReadFileTool(str(tmp_path))], max_tool_calls=1
    )
    with pytest.raises(ToolError, match="final-answer limit"):
        await loop.run(None, request(), refresh_paths=["first.cjs"])


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_recovery_respects_disabled_tools_and_missing_reader(tmp_path, enabled):
    router = Router()
    loop = ToolLoop(
        router,
        [] if enabled else [ReadFileTool(str(tmp_path))],
        max_tool_calls=8 if enabled else 0,
    )
    outcome = await loop.run(None, request(), refresh_paths=["file.cjs"])
    assert outcome.tool_calls == 0
    assert not read_results(router.requests[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event,action", [("pre_tool", "deny"), ("post_tool", "suppress")]
)
async def test_recovery_obeys_operator_hooks_without_exposing_source(
    tmp_path, event, action
):
    (tmp_path / "test.cjs").write_text("private-source-marker")
    router = Router()
    hooks = ToolHookDispatcher(
        (ToolHookRule(id="policy", event=event, action=action),), InMemoryAuditLog()
    )
    outcome = await ToolLoop(router, [ReadFileTool(str(tmp_path))], hooks=hooks).run(
        None, request(), refresh_paths=["test.cjs"]
    )
    assert read_results(router.requests[0])[0].is_error
    assert "private-source-marker" not in router.requests[0].model_dump_json()
    assert "private-source-marker" not in str(outcome.trace)


@pytest.mark.asyncio
async def test_recovery_blocks_sensitive_missing_and_outside_paths(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / ".env").write_text("private-source-marker")
    (tmp_path / "outside.cjs").write_text("private-source-marker")
    router = Router()
    outcome = await ToolLoop(router, [ReadFileTool(str(root))]).run(
        None, request(), refresh_paths=[".env", "missing.cjs", "../outside.cjs"]
    )
    assert all(result.is_error for result in read_results(router.requests[0]))
    assert outcome.tool_calls == 3
    assert "private-source-marker" not in router.requests[0].model_dump_json()


@pytest.mark.asyncio
async def test_recovery_is_bounded_and_reports_truncation(tmp_path):
    for index in range(5):
        (tmp_path / f"{index}.cjs").write_text("x" * 5000)
    router = Router()
    outcome = await ToolLoop(
        router, [ReadFileTool(str(tmp_path), max_output_chars=100)]
    ).run(None, request(), refresh_paths=[f"{index}.cjs" for index in range(5)])
    assert outcome.tool_calls == 4
    assert not router.requests[0].force_final
    assert all(
        "truncado" in result.content for result in read_results(router.requests[0])
    )


@pytest.mark.asyncio
async def test_external_retry_reads_current_file_from_configured_root(tmp_path):
    (tmp_path / "test.cjs").write_text("const currentSource = 2;\n")
    router = Router()
    task = AgentTask(
        title="retry",
        description="retry failed Node test",
        capability=Capability.TESTING,
        acceptance_criteria=["test passes"],
        attempts=[
            TaskAttempt(
                attempt_number=1,
                agent_name="test",
                model="test",
                started_at=datetime.now(timezone.utc),
            )
        ],
        result={
            "workspace": {
                "workspace_root": "/untrusted/checkpoint/root",
                "applied_files": ["test.cjs"],
                "published_files": [{"path": "test.cjs", "content": "staleSource"}],
            }
        },
    )
    executor = LLMExecutor(router, "executor", tools=[ReadFileTool(str(tmp_path))])
    await executor.execute(task, {})
    assert "currentSource" in read_results(router.requests[0])[0].content
    assert "staleSource" not in read_results(router.requests[0])[0].content


@pytest.mark.asyncio
async def test_failed_replace_target_is_refreshed_before_autocorrect(tmp_path):
    (tmp_path / "test.cjs").write_text("const currentSource = 2;\n")
    router = Router(
        {
            "summary": "edit",
            "operations": [
                {
                    "op": "replace",
                    "path": "test.cjs",
                    "search": "not ok 1",
                    "replace": "fixed",
                }
            ],
        }
    )
    executor = LLMExecutor(
        router,
        "executor",
        max_autocorrect_rounds=1,
        workspace_runtime=LocalWorkspaceRuntime(
            str(tmp_path), apply_files_enabled=True
        ),
        tools=[ReadFileTool(str(tmp_path))],
    )
    task = AgentTask(
        title="retry",
        description="fix test",
        capability=Capability.TESTING,
        acceptance_criteria=["test passes"],
    )
    outcome = await executor.execute(task, {})
    assert len(router.requests) == 2
    assert "currentSource" in read_results(router.requests[1])[0].content
    # Reading source never weakens the failed edit gate or invents a repair.
    assert outcome["result"]["workspace"]["apply_errors"]
    assert (
        outcome["result"]["workspace"]["autocorrect"]["stopped_reason"]
        == "max_autocorrect_rounds_exhausted"
    )


@pytest.mark.asyncio
async def test_recovery_prioritizes_failed_and_recent_files(tmp_path):
    names = [f"{index}.cjs" for index in range(6)]
    for name in names:
        (tmp_path / name).write_text(name)
    router = Router()
    task = AgentTask(
        title="retry",
        description="fix test",
        capability=Capability.TESTING,
        acceptance_criteria=["test passes"],
        result={
            "workspace": {
                "apply_errors": [{"path": "2.cjs"}],
                "operation_history": [
                    {"step": "apply_file", "path": name} for name in names
                ],
                "applied_files": names,
            }
        },
    )
    result = await LLMExecutor(
        router, "executor", tools=[ReadFileTool(str(tmp_path))]
    ).execute(task, {})
    assert [
        trace["arguments"]["path"] for trace in result["result"]["exploration"]["trace"]
    ] == ["2.cjs", "5.cjs", "4.cjs", "3.cjs"]


@pytest.mark.asyncio
async def test_exhausted_task_does_not_add_recovery_reads(tmp_path):
    router = Router()
    task = AgentTask(
        title="retry",
        description="fix test",
        capability=Capability.TESTING,
        acceptance_criteria=["test passes"],
        budget={"max_tokens": 10, "consumed_tokens": 10},
        result={"workspace": {"applied_files": ["test.cjs"]}},
    )
    result = await LLMExecutor(
        router, "executor", tools=[ReadFileTool(str(tmp_path))]
    ).execute(task, {})
    assert not read_results(router.requests[0])
    assert result["result"]["exploration"]["tool_calls"] == 0
