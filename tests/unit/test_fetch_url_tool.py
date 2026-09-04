"""fetch_url: exploração web dinâmica com as guardas do coletor, por papel."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.agents.hooks import ToolHookDispatcher, parse_tool_hooks
from app.agents.tools import ToolError, ToolLoop
from app.agents.web_tools import WEB_TOOL_GUIDANCE, FetchUrlTool
from app.api.container import build_agent_tools, web_fetch_roles
from app.infrastructure.audit import InMemoryAuditLog
from app.infrastructure.settings import Settings
from app.infrastructure.web_references import WebReferenceCollector
from app.providers.base import (
    CompletionRequest,
    CompletionResult,
    Message,
    ToolCall,
    Usage,
)

PUBLIC = "93.184.216.34"


async def public_resolver(host: str) -> list[str]:
    return [PUBLIC]


async def private_resolver(host: str) -> list[str]:
    return ["192.168.1.10"]


HTML = (
    "<html><head><title>Docs</title><script>x()</script></head><body>"
    "<nav>menu</nav><main><h1>Instalar</h1><p>Rode uv sync agora.</p></main></body></html>"
)


def make_tool(handler, **overrides) -> FetchUrlTool:
    max_output_chars = overrides.pop("max_output_chars", 12_000)
    overrides.setdefault("resolver", public_resolver)
    collector = WebReferenceCollector(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **overrides
    )
    return FetchUrlTool(collector, max_output_chars=max_output_chars)


@pytest.mark.asyncio
async def test_fetch_url_returns_readable_text_marked_untrusted() -> None:
    tool = make_tool(
        lambda request: httpx.Response(200, text=HTML, headers={"content-type": "text/html"})
    )
    assert tool.name == "fetch_url" and tool.input_schema["required"] == ["url"]
    output = await tool.run({"url": " https://docs.example.com/guia "})
    assert output.startswith("url: https://docs.example.com/guia")
    assert "title: Docs" in output and "content_type: text/html" in output
    assert "NÃO confiável" in output
    assert "Rode uv sync agora." in output
    assert "menu" not in output and "x()" not in output


@pytest.mark.asyncio
async def test_fetch_url_errors_are_tool_errors_with_reason() -> None:
    tool = make_tool(lambda request: httpx.Response(200, text="oi"), resolver=private_resolver)
    with pytest.raises(ToolError) as blocked:
        await tool.run({"url": "https://intranet.example/"})
    assert "não público" in str(blocked.value)

    with pytest.raises(ToolError) as missing:
        await tool.run({})
    assert "`url` é obrigatório" in str(missing.value)

    tool = make_tool(lambda request: httpx.Response(404))
    with pytest.raises(ToolError) as not_found:
        await tool.run({"url": "https://docs.example.com/nada"})
    assert "404" in str(not_found.value)


@pytest.mark.asyncio
async def test_fetch_url_respects_output_limit() -> None:
    tool = make_tool(
        lambda request: httpx.Response(200, text="b" * 3000, headers={"content-type": "text/plain"}),
        max_output_chars=600,
    )
    output = await tool.run({"url": "https://docs.example.com/big"})
    assert len(output) < 700 and "caracteres omitidos" in output


def test_fetch_url_description_lists_allowlist() -> None:
    collector = WebReferenceCollector(allowed_hosts=["docs.example.com"])
    # comparação exata do trecho (não substring de URL: evita o falso positivo
    # py/incomplete-url-substring-sanitization do CodeQL)
    hosts_clause = FetchUrlTool(collector).description.split("Hosts permitidos: ")[1]
    assert hosts_clause.startswith("docs.example.com.")
    open_clause = FetchUrlTool(WebReferenceCollector()).description.split("Hosts permitidos: ")[1]
    assert open_clause.startswith("qualquer host público (80/443).")


def test_build_agent_tools_offers_fetch_url_per_role(tmp_path: Path) -> None:
    disabled = Settings(_env_file=None)
    assert "fetch_url" not in {t.name for t in build_agent_tools(disabled, str(tmp_path), role="executor")}

    enabled = Settings(
        _env_file=None,
        agent_web_fetch_enabled=True,
        web_references_allowed_hosts="docs.example.com",
    )
    assert web_fetch_roles(enabled) == {"planner", "executor"}
    for role in ("planner", "executor"):
        names = [t.name for t in build_agent_tools(enabled, str(tmp_path), role=role)]
        assert names[-1] == "fetch_url" and "read_file" in names
    assert "fetch_url" not in {t.name for t in build_agent_tools(enabled, str(tmp_path), role="judge")}

    judge_too = Settings(
        _env_file=None, agent_web_fetch_enabled=True, agent_web_fetch_roles="judge"
    )
    assert "fetch_url" in {t.name for t in build_agent_tools(judge_too, str(tmp_path), role="judge")}
    assert "fetch_url" not in {t.name for t in build_agent_tools(judge_too, str(tmp_path), role="planner")}

    # desligar tool-use inteiro vence o opt-in da web
    assert build_agent_tools(
        Settings(_env_file=None, agent_tools_enabled=False, agent_web_fetch_enabled=True),
        str(tmp_path),
        role="executor",
    ) == []


class _ScriptedRouter:
    """Router falso: primeiro pede fetch_url, depois emite a resposta final."""

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(self, tier, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        usage = Usage(input_tokens=10, output_tokens=5)
        if len(self.requests) == 1:
            return CompletionResult(
                text="",
                parsed=None,
                tool_calls=[
                    ToolCall(id="c1", name="fetch_url", arguments={"url": "https://docs.example.com/g"})
                ],
                model="m",
                provider="scripted",
                usage=usage,
                cost_usd=0,
                latency_ms=0,
            )
        return CompletionResult(
            text="",
            parsed={"summary": "ok"},
            tool_calls=[],
            model="m",
            provider="scripted",
            usage=usage,
            cost_usd=0,
            latency_ms=0,
        )


@pytest.mark.asyncio
async def test_hooks_can_deny_fetch_url_like_any_tool() -> None:
    tool = make_tool(
        lambda request: httpx.Response(200, text="segredo", headers={"content-type": "text/plain"})
    )
    hooks = ToolHookDispatcher(
        parse_tool_hooks('[{"id":"no-web","event":"pre_tool","tool":"fetch_url","action":"deny"}]'),
        InMemoryAuditLog(),
        timeout_seconds=1.0,
    )
    router = _ScriptedRouter()
    loop = ToolLoop(router, [tool], max_tool_calls=4, hooks=hooks, agent_name="backend_executor")  # type: ignore[arg-type]
    assert loop.has_tool("fetch_url") and not loop.has_tool("read_file")

    outcome = await loop.run(
        None,
        CompletionRequest(
            model="",
            system="s",
            messages=[Message(role="user", content="leia a doc")],
            response_schema=None,
        ),
    )
    [trace] = outcome.exploration_summary()["trace"]
    assert trace["name"] == "fetch_url" and trace["ok"] is False
    # o resultado negado nunca carrega o conteúdo da página
    second_call_messages = router.requests[1].messages
    assert all("segredo" not in (m.content or "") for m in second_call_messages)
    assert all(
        "segredo" not in (r.content or "")
        for m in second_call_messages
        for r in (m.tool_results or [])
    )


def test_guidance_mentions_untrusted_content() -> None:
    assert "fetch_url" in WEB_TOOL_GUIDANCE and "não confiável" in WEB_TOOL_GUIDANCE
