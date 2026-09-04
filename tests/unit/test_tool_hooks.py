"""Hermetic lifecycle policy tests: no real LLM, Docker or external service."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.agents.hooks import (
    HookScope,
    ToolHookCall,
    ToolHookDispatcher,
    ToolHookFailure,
    ToolHookRule,
    parse_tool_hooks,
    tool_hook_scope,
)
from app.agents.tools import ReadFileTool, ToolError, ToolLoop
from app.api.container import LeaseBoundRuntimeFactory, build_container
from app.api.service import WorkflowService
from app.infrastructure.audit import InMemoryAuditLog
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import WorkflowJob
from app.models.factory import RepositoryTarget, WorkspaceLease
from app.models.task import AgentTask, Capability
from app.providers.base import (
    CompletionRequest,
    CompletionResult,
    Message,
    ToolCall,
    Usage,
)
from app.providers.registry import ProviderRouter


class CountingTool:
    name = "read_file"
    description = "Test tool"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, output="private-content", error=None):
        self.output = output
        self.error = error
        self.calls = 0

    async def run(self, arguments):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.output


class Router:
    def __init__(self, batches=None, tokens=1):
        self.batches = batches or [[call()], []]
        self.requests = []
        self.tokens = tokens

    async def complete(self, tier, request):
        self.requests.append(request)
        batch = self.batches[len(self.requests) - 1]
        return CompletionResult(
            text="",
            parsed=None if batch else {"summary": "done"},
            tool_calls=batch,
            model="test",
            provider="test",
            usage=Usage(input_tokens=self.tokens, output_tokens=0),
            cost_usd=0,
            latency_ms=0,
        )


def call(name="read_file", id="model-secret-id"):
    return ToolCall(id=id, name=name, arguments={"path": "private-argument"})


def rule(event="pre_tool", action="audit", **kwargs):
    return ToolHookRule(id=f"{event}-{action}", event=event, action=action, **kwargs)


def make_loop(rules, *, tool=None, router=None, audit=None, timeout=2, limit=8):
    tool = tool or CountingTool()
    router = router or Router()
    audit = audit if audit is not None else InMemoryAuditLog()
    hooks = ToolHookDispatcher(tuple(rules), audit, timeout_seconds=timeout)
    return (
        ToolLoop(
            router,
            [tool],
            hooks=hooks,
            agent_name="backend_executor",
            max_tool_calls=limit,
        ),
        router,
        tool,
        audit,
    )


async def run(loop, **kwargs):
    return await loop.run(
        None,
        CompletionRequest(model="", messages=[Message(role="user", content="test")]),
        **kwargs,
    )


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        "{}",
        "null",
        '[{"id":"x","event":"session_start"}]',
        '[{"id":"x","event":"post_tool","action":"deny"}]',
        '[{"id":"x","event":"pre_tool","action":"suppress"}]',
        '[{"id":"x","event":"pre_tool","command":"echo x"}]',
        '[{"id":"x","event":"pre_tool","output_exceeds_chars":3}]',
        '[{"id":"x","event":"post_tool","output_exceeds_chars":true}]',
        '[{"id":"x","event":"pre_tool"},{"id":"x","event":"pre_tool"}]',
        " " * 65_537,
        json.dumps([{"id": f"x{i}", "event": "pre_tool"} for i in range(65)]),
    ],
)
def test_invalid_operator_config_rejected_at_settings_boundary(value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, tool_hooks_json=value)


def test_empty_rules_default_and_immutable_config():
    assert Settings(_env_file=None).tool_hooks == ()
    config = parse_tool_hooks('[{"id":"x","event":"pre_tool"}]')
    with pytest.raises(ValidationError):
        config[0].action = "deny"


@pytest.mark.asyncio
async def test_pre_deny_wins_over_audit_and_prevents_tool_execution():
    loop, router, tool, audit = make_loop(
        [
            rule(action="deny", tool="read_*", agent="*_executor"),
            rule(),
        ]
    )
    with tool_hook_scope(HookScope("workflow", "project", "owner")):
        result = await run(loop, task_id="task")
    assert tool.calls == 0
    assert result.tool_calls == 1
    feedback = router.requests[1].messages[-1].tool_results[0]
    assert feedback.is_error and "bloqueada" in feedback.content
    records = await audit.list_recent()
    assert len(records) == 1 and records[0].outcome == "denied"
    assert records[0].client_id == "owner"
    assert records[0].workflow_id == "workflow"
    detail = json.loads(records[0].detail)
    assert detail["task_id"] == "task" and len(detail["rules"]) == 2
    serialized = json.dumps([record.to_dict() for record in records])
    assert all(
        value not in serialized
        for value in (
            "private-argument",
            "private-content",
            "model-secret-id",
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("length,suppressed", [(3, False), (4, True)])
async def test_post_suppression_threshold_and_safe_trace(length, suppressed):
    loop, router, tool, audit = make_loop(
        [rule("post_tool", "suppress", output_exceeds_chars=3)],
        tool=CountingTool("X" * length),
    )
    result = await run(loop)
    assert tool.calls == 1
    feedback = router.requests[1].messages[-1].tool_results[0]
    assert feedback.is_error is suppressed
    if suppressed:
        assert "X" * length not in feedback.content
        assert "X" * length not in json.dumps(result.trace)
        assert "não desfeita" in feedback.content
    records = list(reversed(await audit.list_recent()))
    assert [r.action for r in records] == ["tool.pre_tool", "tool.post_tool"]
    assert (
        json.loads(records[0].detail)["run_id"]
        == json.loads(records[1].detail)["run_id"]
    )
    assert records[1].outcome == ("suppressed" if suppressed else "succeeded")


@pytest.mark.asyncio
async def test_case_sensitive_matchers_do_not_block_other_agents():
    loop, _, tool, audit = make_loop([rule(action="deny", agent="BACKEND_*")])
    await run(loop)
    assert tool.calls == 1
    assert all(json.loads(r.detail)["rules"] == [] for r in await audit.list_recent())


@pytest.mark.asyncio
async def test_empty_rules_preserve_behavior_without_audit():
    loop, _, tool, audit = make_loop([])
    await run(loop)
    assert tool.calls == 1 and await audit.list_recent() == []


@pytest.mark.asyncio
async def test_tool_failure_emits_error_not_post_and_redacts_hook_audit():
    loop, _, tool, audit = make_loop(
        [rule("tool_error")],
        tool=CountingTool(error=RuntimeError("private-error")),
    )
    await run(loop)
    records = list(reversed(await audit.list_recent()))
    assert tool.calls == 1
    assert [r.action for r in records] == ["tool.pre_tool", "tool.tool_error"]
    assert records[-1].outcome == "failed"
    assert "private-error" not in json.dumps([r.to_dict() for r in records])


@pytest.mark.asyncio
async def test_unknown_tool_names_not_copied_to_audit():
    loop, _, tool, audit = make_loop(
        [rule()], router=Router([[call("secret-name")], []])
    )
    await run(loop)
    assert tool.calls == 0
    assert all(
        json.loads(r.detail)["tool"] == "<unknown>" for r in await audit.list_recent()
    )


class UnavailableAudit:
    def __init__(self, fail_at, timeout):
        self.fail_at = fail_at
        self.timeout = timeout
        self.calls = 0

    async def record(self, event):
        self.calls += 1
        if self.calls == self.fail_at:
            if self.timeout:
                await asyncio.Event().wait()
            raise RuntimeError("private-sink-error")


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", [1, 2])
@pytest.mark.parametrize("timeout", [False, True])
async def test_audit_failure_or_timeout_fails_closed(fail_at, timeout):
    audit = UnavailableAudit(fail_at, timeout)
    loop, router, tool, _ = make_loop([rule()], audit=audit, timeout=0.01)
    with pytest.raises(ToolHookFailure) as raised:
        await run(loop)
    assert "private" not in str(raised.value)
    assert tool.calls == fail_at - 1
    assert len(router.requests) == 1  # no tool result forwarded to model


@pytest.mark.asyncio
async def test_cancellation_propagates_without_post_success():
    loop, _, _, audit = make_loop(
        [rule()], tool=CountingTool(error=asyncio.CancelledError())
    )
    with pytest.raises(asyncio.CancelledError):
        await run(loop)
    assert [r.action for r in await audit.list_recent()] == ["tool.pre_tool"]


@pytest.mark.asyncio
async def test_builtin_path_policy_cannot_be_overridden(tmp_path):
    (tmp_path / ".env").write_text("PRIVATE=secret")
    router = Router(
        [[ToolCall(id="x", name="read_file", arguments={"path": ".env"})], []]
    )
    loop, _, _, audit = make_loop(
        [rule()], router=router, tool=ReadFileTool(str(tmp_path))
    )
    result = await run(loop)
    assert not result.trace[0]["ok"]
    assert "sensível" in result.trace[0]["preview"]
    assert "PRIVATE" not in json.dumps(result.trace)
    assert (await audit.list_recent())[0].action == "tool.tool_error"


@pytest.mark.asyncio
async def test_oversized_batches_enforce_limit_before_each_call():
    router = Router([[call(id=str(i)) for i in range(5)], []])
    loop, _, tool, audit = make_loop([rule()], router=router, limit=2)
    result = await run(loop)
    assert result.tool_calls == tool.calls == 2
    results = router.requests[1].messages[-1].tool_results
    assert [r.is_error for r in results] == [False, False, True, True, True]
    assert len(await audit.list_recent()) == 4
    assert router.requests[1].force_final


@pytest.mark.asyncio
async def test_denied_calls_consume_limit_and_provider_cannot_ignore_stop():
    router = Router([[call()], [call()], []])
    loop, _, tool, _ = make_loop([rule(action="deny")], router=router, limit=1)
    with pytest.raises(ToolError, match="final-answer limit"):
        await run(loop)
    assert tool.calls == 0 and len(router.requests) == 2


@pytest.mark.asyncio
async def test_exhausted_token_budget_does_not_execute_batch():
    loop, router, tool, audit = make_loop([rule()], router=Router(tokens=100))
    result = await run(loop, token_ceiling=50)
    assert result.stopped_reason == "token_ceiling"
    assert result.tool_calls == tool.calls == 0
    assert await audit.list_recent() == []
    assert router.requests[-1].force_final


def test_container_and_lease_agents_share_dispatcher(monkeypatch, tmp_path):
    import app.api.container as composition

    captured = {}
    router = Mock(spec=ProviderRouter)
    router.escalate.side_effect = lambda tier: tier
    monkeypatch.setattr(composition, "build_provider_router", lambda *a, **kw: router)
    monkeypatch.setattr(composition, "build_workflow", lambda **kw: captured.update(kw))
    settings = Settings(
        _env_file=None, tool_hooks_json='[{"id":"audit","event":"pre_tool"}]'
    )
    build_container(settings, None, None, False)
    hooks = captured["planner"]._tool_loop._hooks
    assert hooks is not None and captured["judge"]._tool_loop._hooks is hooks
    registries = [captured["registry"]]
    lease = WorkspaceLease(
        workflow_id="w",
        repository=RepositoryTarget(full_name="test/repo"),
        local_path=str(tmp_path),
        branch="forgehand/w",
        base_sha="a" * 40,
    )
    factory = LeaseBoundRuntimeFactory(settings, router, hooks=hooks)
    assert factory.build_planner(lease)._tool_loop._hooks is hooks
    assert factory.build_judge(lease)._tool_loop._hooks is hooks
    registries.append(factory.build_registry(lease))
    for registry in registries:
        for capability in Capability:
            for escalated in (False, True):
                task = AgentTask(
                    title="test",
                    description="test hooks",
                    capability=capability,
                    tier_escalated=escalated,
                    acceptance_criteria=["test"],
                )
                assert registry.select(task)._tool_loop._hooks is hooks


@pytest.mark.asyncio
async def test_service_scope_is_trusted_concurrent_and_reset_after_failure():
    audit = InMemoryAuditLog()
    hooks = ToolHookDispatcher((rule(),), audit)
    arrived = 0
    both = asyncio.Event()

    class Graph:
        async def ainvoke(self, payload, config):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both.set()
            await both.wait()
            call_context = ToolHookCall(
                config["configurable"]["thread_id"], 1, "read_file", "planner"
            )
            await hooks.dispatch("pre_tool", call_context)
            if config["configurable"]["thread_id"] == "one":
                raise RuntimeError("test")

    service = WorkflowService(Graph(), Settings(_env_file=None), None, False)
    job = WorkflowJob(
        id="job-one",
        workflow_id="one",
        project_id="project-one",
        owner_client_id="owner-one",
        kind="start",
        payload={"client_id": "forged-owner"},
    )
    results = await asyncio.gather(
        service._invoke_job(job),
        service._invoke_job(
            replace(
                job,
                id="job-two",
                workflow_id="two",
                project_id="project-two",
                owner_client_id="owner-two",
            )
        ),
        return_exceptions=True,
    )
    assert isinstance(results[0], RuntimeError) and results[1] is None
    records = await audit.list_recent()
    assert {(r.workflow_id, r.project_id, r.client_id) for r in records} == {
        ("one", "project-one", "owner-one"),
        ("two", "project-two", "owner-two"),
    }
    with pytest.raises(asyncio.CancelledError):
        with tool_hook_scope(HookScope("cancelled", "p", "c")):
            raise asyncio.CancelledError()
    await hooks.dispatch("pre_tool", ToolHookCall("outside", 1, "read_file", "planner"))
    outside = (await audit.list_recent())[0]
    assert outside.workflow_id is None and outside.client_id is None


@pytest.mark.asyncio
async def test_real_graph_applies_hooks_across_planner_executor_and_judge(
    monkeypatch, tmp_path
):
    """Actual graph/checkpoint/service; only the paid provider is replaced."""
    import app.api.container as composition
    from langgraph.checkpoint.memory import MemorySaver
    from app.graph.state import WorkflowState
    from app.graph.workflow import build_serde
    from app.infrastructure.workflow_queue import InMemoryWorkflowQueue

    (tmp_path / "README.md").write_text("Local fixture documentation")

    class GraphRouter:
        def __init__(self):
            self.requests = []

        def escalate(self, tier):
            return tier

        async def complete(self, tier, request):
            self.requests.append(request)
            parsed = None
            tool_calls = []
            if not any(message.tool_results for message in request.messages):
                tool_calls = [
                    ToolCall(
                        id="read", name="read_file", arguments={"path": "README.md"}
                    )
                ]
            elif request.response_schema.__name__ == "PlanOutput":
                parsed = {
                    "rationale": "Read-only documentation task",
                    "tasks": [
                        {
                            "title": "Explain usage",
                            "description": "Explain the local README",
                            "capability": "documentation",
                            "acceptance_criteria": ["Usage explained"],
                            "depends_on": [],
                            "is_critical": False,
                        }
                    ],
                }
            elif request.response_schema.__name__ == "ExecutionOutput":
                parsed = {"summary": "Usage explained", "operations": [], "notes": []}
            elif request.response_schema.__name__ == "JudgeOutput":
                parsed = {
                    "criteria": [
                        {
                            "criterion": "Usage explained",
                            "score": 1,
                            "reasoning": "Verified",
                        }
                    ],
                    "failures": [],
                    "required_changes": [],
                    "overall_score": 1,
                    "approved": True,
                }
            else:
                raise AssertionError("Unexpected model request")
            return CompletionResult(
                text="",
                parsed=parsed,
                tool_calls=tool_calls,
                model="test",
                provider="test",
                usage=Usage(input_tokens=1, output_tokens=1),
                cost_usd=0,
                latency_ms=0,
            )

    router = GraphRouter()
    monkeypatch.setattr(composition, "build_provider_router", lambda *a, **kw: router)
    settings = Settings(
        _env_file=None,
        repository_root=str(tmp_path),
        executor_workspace_root=str(tmp_path),
        judge_independence="off",
        judge_critical_quorum=1,
        tool_hooks_json='[{"id":"deny-executor-read","event":"pre_tool","agent":"docs_executor","action":"deny"}]',
    )
    container = build_container(
        settings,
        MemorySaver(serde=build_serde()),
        InMemoryWorkflowQueue(),
        False,
    )
    state = WorkflowState(
        workflow_id="real-graph",
        project_id="p",
        owner_client_id="owner",
        request="Explain how the documented application works",
    )
    service = container.workflow_service
    try:
        await service._invoke_job(
            WorkflowJob(
                id="job",
                workflow_id=state.workflow_id,
                project_id="p",
                owner_client_id="owner",
                kind="start",
                payload=state.model_dump(),
            )
        )
        snapshot = await service._app.aget_state(service._config(state.workflow_id))
        assert snapshot.values["phase"] == "completed"
        records = await container.audit_log.list_recent()
        by_agent = {}
        for record in records:
            detail = json.loads(record.detail)
            by_agent.setdefault(detail["agent"], []).append(record)
            assert record.workflow_id == "real-graph" and record.client_id == "owner"
        assert set(by_agent) == {"planner", "docs_executor", "judge"}
        assert [record.outcome for record in by_agent["docs_executor"]] == ["denied"]
        assert all(json.loads(r.detail)["task_id"] for r in by_agent["judge"])
        assert len(router.requests) == 6
    finally:
        await service.shutdown()
