"""Ferramentas MCP via stdio contra um servidor de teste real (subprocesso)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents.mcp_tools import (
    McpStdioClient,
    discover_mcp_tools,
    discover_mcp_tools_async,
    parse_mcp_servers,
)
from app.agents.tools import ToolError
from app.api.container import build_agent_tools
from app.infrastructure.settings import Settings

SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_echo_server.py"


def _config(**extra) -> str:
    return json.dumps([{"name": "echo", "command": sys.executable, "args": [str(SERVER)], **extra}])


def test_parse_config_validates_names_and_duplicates() -> None:
    assert parse_mcp_servers("[]") == [] and parse_mcp_servers("") == []
    [server] = parse_mcp_servers(_config(allowed_tools=["echo"]))
    assert server.name == "echo" and server.allowed_tools == ["echo"]
    with pytest.raises(ValidationError):
        parse_mcp_servers(json.dumps([{"name": "Nome Inválido", "command": "x"}]))
    with pytest.raises(ValueError):
        parse_mcp_servers(json.dumps([{"name": "a", "command": "x"}, {"name": "a", "command": "y"}]))


@pytest.mark.asyncio
async def test_discovery_call_error_and_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-nao-vazar")
    tools = await discover_mcp_tools_async(parse_mcp_servers(_config()), timeout_seconds=20)
    by_name = {tool.name: tool for tool in tools}
    assert {"mcp_echo_echo", "mcp_echo_secret_env", "mcp_echo_fail", "mcp_echo_hidden"} <= set(by_name)
    echo = by_name["mcp_echo_echo"]
    assert echo.input_schema["required"] == ["text"] and "[MCP echo]" in echo.description

    assert await echo.run({"text": "olá"}) == "eco: olá"
    # o servidor não herda segredos do controlador
    assert await by_name["mcp_echo_secret_env"].run({}) == "ausente"
    with pytest.raises(ToolError, match="quebrou"):
        await by_name["mcp_echo_fail"].run({})

    filtered = await discover_mcp_tools_async(parse_mcp_servers(_config(allowed_tools=["echo"])), timeout_seconds=20)
    assert [tool.name for tool in filtered] == ["mcp_echo_echo"]


@pytest.mark.asyncio
async def test_unknown_tool_and_broken_command_are_errors() -> None:
    [server] = parse_mcp_servers(_config())
    client = McpStdioClient(server, timeout_seconds=20)
    with pytest.raises(Exception, match="desconhecida"):
        await client.call_tool("nao_existe", {})
    [broken] = parse_mcp_servers(json.dumps([{"name": "b", "command": "executavel-que-nao-existe-xyz"}]))
    with pytest.raises(Exception, match="não foi possível iniciar"):
        await McpStdioClient(broken, timeout_seconds=5).list_tools()


def test_container_offers_mcp_tools_per_role(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, mcp_servers_json=_config(allowed_tools=["echo"]), mcp_timeout_seconds=20)
    for role in ("planner", "executor"):
        names = [tool.name for tool in build_agent_tools(settings, str(tmp_path), role=role)]
        assert "mcp_echo_echo" in names and "read_file" in names
    assert "mcp_echo_echo" not in [t.name for t in build_agent_tools(settings, str(tmp_path), role="judge")]
    # memoizado por configuração: segunda descoberta não relança o servidor
    assert discover_mcp_tools(settings.mcp_servers_json, timeout_seconds=20) is not None
    assert build_agent_tools(Settings(_env_file=None), str(tmp_path), role="executor")
