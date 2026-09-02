"""Tool-use nos agentes: ferramentas sandboxed, loop com tetos, mapeamento nos
providers e integração no executor/judge/planner."""

from __future__ import annotations

import json
from pathlib import Path

import anthropic
import httpx
import pytest

from app.agents.executor import LLMExecutor
from app.agents.judge import LLMJudge
from app.agents.planner import LLMPlanner
from app.agents.tools import (
    ListDirectoryTool,
    ReadFileTool,
    RunCheckTool,
    SearchRepositoryTool,
    ToolError,
    ToolLoop,
    build_workspace_tools,
)
from app.agents.validation import ValidationSignal
from app.api.container import build_agent_tools
from app.infrastructure.settings import Settings
from app.models.task import AgentTask, Capability
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import (
    CompletionRequest,
    CompletionResult,
    Message,
    ModelPricing,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)
from app.providers.openai_compatible import OpenAICompatibleProvider
from pydantic import BaseModel

PRICING = {
    "claude-sonnet-5": ModelPricing(input_per_mtok=3.0, output_per_mtok=15.0),
    "openai/gpt-4o-mini": ModelPricing(input_per_mtok=0.15, output_per_mtok=0.60),
}


class Answer(BaseModel):
    summary: str


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "svc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x\n", encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------
# Ferramentas
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_numbers_lines_and_supports_ranges(tmp_path: Path):
    tool = ReadFileTool(str(_workspace(tmp_path)))
    out = await tool.run({"path": "app/svc.py"})
    assert out.startswith("app/svc.py (6 linhas)")
    assert "1| def add(a, b):" in out
    ranged = await tool.run({"path": "app/svc.py", "start_line": 5, "end_line": 6})
    assert "5| def sub(a, b):" in ranged
    assert "def add" not in ranged


@pytest.mark.asyncio
async def test_read_file_blocks_traversal_sensitive_and_ignored(tmp_path: Path):
    tool = ReadFileTool(str(_workspace(tmp_path)))
    with pytest.raises(ToolError, match="fora do workspace"):
        await tool.run({"path": "../outside.txt"})
    with pytest.raises(ToolError, match="sensível"):
        await tool.run({"path": ".env"})
    with pytest.raises(ToolError, match="ignorado"):
        await tool.run({"path": ".git/config"})
    with pytest.raises(ToolError, match="não encontrado"):
        await tool.run({"path": "app/missing.py"})
    with pytest.raises(ToolError, match="diretório"):
        await tool.run({"path": "app"})


@pytest.mark.asyncio
async def test_read_file_truncates_large_output(tmp_path: Path):
    (tmp_path / "big.txt").write_text("x" * 5000 + "\n", encoding="utf-8")
    tool = ReadFileTool(str(tmp_path), max_output_chars=500)
    out = await tool.run({"path": "big.txt"})
    assert len(out) < 700
    assert "[truncado:" in out


@pytest.mark.asyncio
async def test_list_directory_hides_ignored_and_sensitive(tmp_path: Path):
    tool = ListDirectoryTool(str(_workspace(tmp_path)))
    out = await tool.run({})
    assert out.splitlines()[0] == "."
    assert "app/" in out and "README.md" in out
    assert ".env" not in out and ".git" not in out
    with pytest.raises(ToolError, match="não é um diretório"):
        await tool.run({"path": "README.md"})


@pytest.mark.asyncio
async def test_search_repository_returns_path_line_and_respects_limits(tmp_path: Path):
    tool = SearchRepositoryTool(str(_workspace(tmp_path)))
    out = await tool.run({"pattern": r"def (add|sub)"})
    assert "2 ocorrência(s)" in out
    assert "app/svc.py:1: def add(a, b):" in out
    assert "app/svc.py:5: def sub(a, b):" in out
    limited = await tool.run({"pattern": r"def ", "max_results": 1})
    assert "limite 1 atingido" in limited
    none = await tool.run({"pattern": "SECRET"})
    assert none.startswith("nenhuma ocorrência")  # .env não é varrido
    with pytest.raises(ToolError, match="regex inválida"):
        await tool.run({"pattern": "("})


class FakeCheck:
    def __init__(self, name: str, passed: bool) -> None:
        self.name = name
        self._passed = passed
        self.calls = 0

    async def execute(self) -> ValidationSignal:
        self.calls += 1
        return ValidationSignal(
            name=self.name,
            passed=self._passed,
            command=f"uv run {self.name}",
            exit_code=0 if self._passed else 1,
            stdout="ok" if self._passed else "",
            stderr="" if self._passed else "E   assert 1 == 2",
        )


@pytest.mark.asyncio
async def test_run_check_only_runs_configured_validators():
    pytest_check = FakeCheck("pytest", passed=False)
    tool = RunCheckTool([pytest_check, FakeCheck("ruff", passed=True)])
    assert tool.input_schema["properties"]["name"]["enum"] == ["pytest", "ruff"]
    out = await tool.run({"name": "pytest"})
    assert out.startswith("pytest: failed (exit_code=1)")
    assert "assert 1 == 2" in out
    assert pytest_check.calls == 1
    with pytest.raises(ToolError, match="desconhecida"):
        await tool.run({"name": "rm"})


def test_build_workspace_tools_adds_run_check_only_with_validators(tmp_path: Path):
    names = [t.name for t in build_workspace_tools(str(tmp_path))]
    assert names == ["read_file", "list_directory", "search_repository"]
    with_checks = build_workspace_tools(
        str(tmp_path), validators=[FakeCheck("ruff", True)]
    )
    assert [t.name for t in with_checks][-1] == "run_check"


def test_build_agent_tools_respects_settings(tmp_path: Path):
    assert build_agent_tools(Settings(agent_tools_enabled=False), str(tmp_path)) == []
    without_checks = build_agent_tools(
        Settings(agent_tools_allow_checks=False),
        str(tmp_path),
        validators=[FakeCheck("ruff", True)],  # type: ignore[list-item]
    )
    assert "run_check" not in [t.name for t in without_checks]


# --------------------------------------------------------------------------
# ToolLoop
# --------------------------------------------------------------------------


class ScriptedRouter:
    """Cada entrada do script é (tool_calls, parsed). Registra os requests."""

    def __init__(self, script, tokens_per_call: int = 10):
        self._script = list(script)
        self.requests: list[CompletionRequest] = []
        self._tokens = tokens_per_call

    async def complete(self, tier, request):
        self.requests.append(request)
        tool_calls, parsed = self._script.pop(0)
        return CompletionResult(
            text="",
            parsed=parsed,
            tool_calls=tool_calls,
            model="fake",
            provider="fake",
            usage=Usage(input_tokens=self._tokens, output_tokens=0),
            cost_usd=0.01,
            latency_ms=1.0,
        )


def _request() -> CompletionRequest:
    return CompletionRequest(
        model="",
        system="sys",
        messages=[Message(role="user", content="Tarefa: x")],
        response_schema=Answer,
    )


@pytest.mark.asyncio
async def test_tool_loop_executes_calls_and_feeds_results_back(tmp_path: Path):
    _workspace(tmp_path)
    router = ScriptedRouter(
        [
            (
                [
                    ToolCall(
                        id="c1", name="read_file", arguments={"path": "README.md"}
                    ),
                    ToolCall(id="c2", name="read_file", arguments={"path": ".env"}),
                ],
                None,
            ),
            ([], {"summary": "pronto"}),
        ]
    )
    loop = ToolLoop(router, build_workspace_tools(str(tmp_path)), max_tool_calls=8)

    outcome = await loop.run(None, _request())

    assert outcome.result.parsed == {"summary": "pronto"}
    assert outcome.tokens == 20 and outcome.cost_usd == pytest.approx(0.02)
    assert outcome.tool_calls == 2 and outcome.rounds == 2
    assert outcome.stopped_reason == "final_answer"
    assert [t["ok"] for t in outcome.trace] == [True, False]
    assert "sensível" in outcome.trace[1]["preview"]

    first, second = router.requests
    assert [spec.name for spec in first.tools] == [
        "read_file",
        "list_directory",
        "search_repository",
    ]
    assert first.force_final is False
    # segunda rodada: histórico com assistant(tool_calls) + user(tool_results)
    assert second.messages[1].role == "assistant"
    assert second.messages[1].tool_calls[0].id == "c1"
    results = second.messages[2].tool_results
    assert results[0].tool_call_id == "c1" and results[0].is_error is False
    assert "# demo" in results[0].content
    assert results[1].is_error is True
    assert second.messages[2].content == ""


@pytest.mark.asyncio
async def test_tool_loop_forces_final_answer_at_max_calls(tmp_path: Path):
    _workspace(tmp_path)
    router = ScriptedRouter(
        [
            ([ToolCall(id="c1", name="list_directory", arguments={})], None),
            ([ToolCall(id="c2", name="list_directory", arguments={})], None),
            ([], {"summary": "forçado"}),
        ]
    )
    loop = ToolLoop(router, build_workspace_tools(str(tmp_path)), max_tool_calls=2)

    outcome = await loop.run(None, _request())

    assert outcome.stopped_reason == "max_tool_calls"
    assert outcome.tool_calls == 2
    final_request = router.requests[-1]
    assert final_request.force_final is True
    assert final_request.tools, "definições mantidas na rodada final"
    assert "Limite de exploração" in final_request.messages[-1].content


@pytest.mark.asyncio
async def test_tool_loop_forces_final_answer_at_token_ceiling(tmp_path: Path):
    _workspace(tmp_path)
    router = ScriptedRouter(
        [
            ([ToolCall(id="c1", name="list_directory", arguments={})], None),
            ([], {"summary": "ok"}),
        ],
        tokens_per_call=100,
    )
    loop = ToolLoop(router, build_workspace_tools(str(tmp_path)), max_tool_calls=8)

    outcome = await loop.run(None, _request(), token_ceiling=50)

    assert outcome.stopped_reason == "token_ceiling"
    assert router.requests[-1].force_final is True


@pytest.mark.asyncio
async def test_tool_loop_reports_unknown_tool_as_error():
    router = ScriptedRouter(
        [
            ([ToolCall(id="c1", name="delete_everything", arguments={})], None),
            ([], {"summary": "ok"}),
        ]
    )
    loop = ToolLoop(router, [ListDirectoryTool(".")], max_tool_calls=8)
    outcome = await loop.run(None, _request())
    result = router.requests[1].messages[2].tool_results[0]
    assert result.is_error is True
    assert "desconhecida" in result.content
    assert outcome.trace[0]["ok"] is False


@pytest.mark.asyncio
async def test_tool_loop_without_tools_is_a_single_call():
    router = ScriptedRouter([([], {"summary": "direto"})])
    loop = ToolLoop(router, None)
    assert loop.has_tools is False
    outcome = await loop.run(None, _request())
    assert outcome.stopped_reason == "no_tools"
    assert router.requests[0].tools == []


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------

SPECS = [
    ToolSpec(
        name="read_file",
        description="lê",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
]


def _anthropic_client(handler):
    return anthropic.AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _anthropic_response(content):
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": content,
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )


@pytest.mark.asyncio
async def test_anthropic_lists_emit_first_and_returns_tool_calls():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _anthropic_response(
            [
                {"type": "text", "text": "vou ler"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_file",
                    "input": {"path": "a.py"},
                },
            ]
        )

    provider = AnthropicProvider(PRICING, client=_anthropic_client(handler))
    result = await provider.complete(
        _request().model_copy(update={"model": "claude-sonnet-5", "tools": SPECS})
    )

    assert [t["name"] for t in seen["tools"]] == ["emit_structured_output", "read_file"]
    assert seen["tool_choice"] == {"type": "any"}
    assert result.parsed is None
    assert result.tool_calls == [
        ToolCall(id="toolu_1", name="read_file", arguments={"path": "a.py"})
    ]
    assert result.text == "vou ler"


@pytest.mark.asyncio
async def test_anthropic_force_final_targets_emit_and_maps_tool_history():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _anthropic_response(
            [
                {
                    "type": "tool_use",
                    "id": "toolu_2",
                    "name": "emit_structured_output",
                    "input": {"summary": "fim"},
                }
            ]
        )

    provider = AnthropicProvider(PRICING, client=_anthropic_client(handler))
    history = [
        Message(role="user", content="Tarefa: x"),
        Message(
            role="assistant",
            content="lendo",
            tool_calls=[
                ToolCall(id="toolu_1", name="read_file", arguments={"path": "a"})
            ],
        ),
        Message(
            role="user",
            content="Limite.",
            tool_results=[
                ToolResult(
                    tool_call_id="toolu_1", name="read_file", content="x", is_error=True
                )
            ],
        ),
    ]
    result = await provider.complete(
        CompletionRequest(
            model="claude-sonnet-5",
            system="sys",
            messages=history,
            response_schema=Answer,
            tools=SPECS,
            force_final=True,
        )
    )

    assert seen["tool_choice"] == {"type": "tool", "name": "emit_structured_output"}
    assert len(seen["tools"]) == 2
    assistant = seen["messages"][1]
    assert assistant["content"][0] == {"type": "text", "text": "lendo"}
    assert assistant["content"][1]["type"] == "tool_use"
    assert assistant["content"][1]["id"] == "toolu_1"
    user = seen["messages"][2]
    assert user["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": "x",
        "is_error": True,
    }
    assert user["content"][1] == {"type": "text", "text": "Limite."}
    assert result.parsed == {"summary": "fim"}
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_anthropic_without_tools_keeps_forced_emit():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _anthropic_response(
            [
                {
                    "type": "tool_use",
                    "id": "t",
                    "name": "emit_structured_output",
                    "input": {"summary": "s"},
                }
            ]
        )

    provider = AnthropicProvider(PRICING, client=_anthropic_client(handler))
    await provider.complete(_request().model_copy(update={"model": "claude-sonnet-5"}))
    assert seen["tool_choice"] == {"type": "tool", "name": "emit_structured_output"}
    assert seen["messages"] == [{"role": "user", "content": "Tarefa: x"}]


def _openai_provider(handler):
    return OpenAICompatibleProvider(
        PRICING,
        base_url="https://router.test",
        provider_name="openrouter",
        client=httpx.AsyncClient(
            base_url="https://router.test", transport=httpx.MockTransport(handler)
        ),
    )


def _openai_response(message):
    return httpx.Response(
        200,
        json={
            "model": "openai/gpt-4o-mini",
            "choices": [{"message": message, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


@pytest.mark.asyncio
async def test_openai_compatible_uses_functions_and_parses_tool_calls():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _openai_response(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "a.py"}),
                        },
                    }
                ],
            }
        )

    result = await _openai_provider(handler).complete(
        _request().model_copy(update={"model": "openai/gpt-4o-mini", "tools": SPECS})
    )

    assert "response_format" not in seen
    assert seen["tool_choice"] == "required"
    functions = [t["function"]["name"] for t in seen["tools"]]
    assert functions == ["emit_structured_output", "read_file"]
    assert seen["tools"][0]["function"]["strict"] is True
    assert result.parsed is None
    assert result.tool_calls == [
        ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"})
    ]


@pytest.mark.asyncio
async def test_openai_compatible_force_final_and_tool_history():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _openai_response(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "emit_structured_output",
                            "arguments": json.dumps({"summary": "fim"}),
                        },
                    }
                ],
            }
        )

    history = [
        Message(role="user", content="Tarefa: x"),
        Message(
            role="assistant",
            tool_calls=[
                ToolCall(id="call_1", name="read_file", arguments={"path": "a"})
            ],
        ),
        Message(
            role="user",
            content="Limite.",
            tool_results=[
                ToolResult(tool_call_id="call_1", name="read_file", content="x")
            ],
        ),
    ]
    result = await _openai_provider(handler).complete(
        CompletionRequest(
            model="openai/gpt-4o-mini",
            system="sys",
            messages=history,
            response_schema=Answer,
            tools=SPECS,
            force_final=True,
        )
    )

    assert seen["tool_choice"] == {
        "type": "function",
        "function": {"name": "emit_structured_output"},
    }
    messages = seen["messages"]
    assert messages[0]["role"] == "system"
    assistant = messages[2]
    assert assistant["role"] == "assistant" and assistant["content"] is None
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"path": "a"}'
    assert messages[3] == {"role": "tool", "tool_call_id": "call_1", "content": "x"}
    assert messages[4] == {"role": "user", "content": "Limite."}
    assert result.parsed == {"summary": "fim"}


@pytest.mark.asyncio
async def test_openai_compatible_without_tools_keeps_response_format():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _openai_response({"content": json.dumps({"summary": "s"})})

    result = await _openai_provider(handler).complete(
        _request().model_copy(update={"model": "openai/gpt-4o-mini"})
    )
    assert "tools" not in seen
    assert seen["response_format"]["type"] == "json_schema"
    assert result.parsed == {"summary": "s"}


# --------------------------------------------------------------------------
# Integração nos agentes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_explores_then_emits_and_records_exploration(tmp_path: Path):
    _workspace(tmp_path)
    router = ScriptedRouter(
        [
            (
                [ToolCall(id="c1", name="read_file", arguments={"path": "app/svc.py"})],
                None,
            ),
            (
                [],
                {
                    "summary": "ok",
                    "operations": [
                        {
                            "op": "replace",
                            "path": "app/svc.py",
                            "search": "    return a - b\n",
                            "replace": "    return a - b  # sub\n",
                        }
                    ],
                },
            ),
        ]
    )
    executor = LLMExecutor(
        router,
        agent_name="backend_executor",
        tools=build_workspace_tools(str(tmp_path)),
        max_tool_calls=4,
    )
    task = AgentTask(
        title="backend",
        description="editar",
        capability=Capability.BACKEND,
        acceptance_criteria=["ok"],
    )

    outcome = await executor.execute(task, {})

    assert "Ferramentas disponíveis" in router.requests[0].system
    exploration = outcome["result"]["exploration"]
    assert exploration["tool_calls"] == 1
    assert exploration["trace"][0]["name"] == "read_file"
    assert "def sub" in exploration["trace"][0]["preview"]
    assert outcome["tokens"] == 20
    assert outcome["cost_usd"] == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_executor_without_tools_has_no_guidance_nor_exploration():
    router = ScriptedRouter([([], {"summary": "ok", "operations": []})])
    executor = LLMExecutor(router, agent_name="backend_executor")
    task = AgentTask(
        title="t",
        description="d",
        capability=Capability.BACKEND,
        acceptance_criteria=["c"],
    )
    outcome = await executor.execute(task, {})
    assert "Ferramentas disponíveis" not in router.requests[0].system
    assert "exploration" not in outcome["result"]


@pytest.mark.asyncio
async def test_judge_uses_tools_and_aggregates_usage(tmp_path: Path):
    _workspace(tmp_path)
    verdict = {
        "criteria": [{"criterion": "c", "score": 1.0, "reasoning": "ok"}],
        "failures": [],
        "required_changes": [],
        "overall_score": 1.0,
        "approved": True,
    }
    router = ScriptedRouter(
        [
            (
                [ToolCall(id="c1", name="read_file", arguments={"path": "README.md"})],
                None,
            ),
            ([], verdict),
        ]
    )
    judge = LLMJudge(
        router, tools=build_workspace_tools(str(tmp_path)), max_tool_calls=2
    )
    task = AgentTask(
        title="t",
        description="d",
        capability=Capability.DOCUMENTATION,
        acceptance_criteria=["c"],
        result={"summary": "s"},
    )
    outcome = await judge.evaluate(task, {})
    assert outcome.evaluation.approved is True
    assert outcome.usage.tokens == 20
    assert "CONFIRA o conteúdo real" in router.requests[0].system


@pytest.mark.asyncio
async def test_planner_uses_tools(tmp_path: Path):
    _workspace(tmp_path)
    plan = {
        "rationale": "r",
        "tasks": [
            {
                "title": "t",
                "description": "d",
                "capability": "backend",
                "acceptance_criteria": ["c"],
                "evidence_ids": [],
                "depends_on": [],
                "is_critical": False,
            }
        ],
    }
    router = ScriptedRouter(
        [
            ([ToolCall(id="c1", name="list_directory", arguments={})], None),
            ([], plan),
        ]
    )
    planner = LLMPlanner(
        router, tools=build_workspace_tools(str(tmp_path)), max_tool_calls=2
    )
    outcome = await planner.create_plan("fazer algo útil", {})
    assert len(outcome.plan) == 1
    assert outcome.usage.tokens == 20
    assert "Ferramentas disponíveis" in router.requests[0].system
