"""run_command: allowlist, subcomandos negados, timeout e ambiente sem segredos."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app.agents.command_tool import COMMAND_TOOL_GUIDANCE, RunCommandTool, check_command_policy
from app.agents.tools import ToolError
from app.api.container import build_agent_tools
from app.infrastructure.settings import Settings
from app.infrastructure.workspace_runtime import (
    LocalCommandRunner,
    sanitized_environment,
)


class _FakeRunner:
    def __init__(self, exit_code: int = 0, stdout: str = "ok", stderr: str = "", timed_out: bool = False):
        self.calls: list[tuple[str, Path]] = []
        self.result = {"exit_code": exit_code, "stdout": stdout, "stderr": stderr, "timed_out": timed_out}

    async def run(self, command: str, workspace_root: Path, output_limit: int):
        self.calls.append((command, workspace_root))
        return {"command": command, **self.result}


@pytest.mark.asyncio
async def test_allowed_command_runs_in_workspace(tmp_path: Path) -> None:
    runner = _FakeRunner(stdout="5 passed")
    tool = RunCommandTool(runner, tmp_path)
    output = await tool.run({"command": "python -m pytest tests/test_core.py -q"})
    assert output.startswith("python -m pytest tests/test_core.py -q: passed (exit_code=0)")
    assert "5 passed" in output
    assert runner.calls == [("python -m pytest tests/test_core.py -q", tmp_path.resolve())]


@pytest.mark.asyncio
async def test_disallowed_executables_and_subcommands_are_tool_errors(tmp_path: Path) -> None:
    runner = _FakeRunner()
    tool = RunCommandTool(runner, tmp_path)
    for command, fragment in [
        ("curl https://example.com", "não permitido"),
        ("rm -rf .", "não permitido"),
        ("git push origin main", "git push"),
        ("git -C . fetch --all", "git fetch"),
        ("uv add requests", "uv add"),
        ("uv pip install requests", "uv pip"),
        ("python -m pip install x", "python -m pip"),
        ("python -c 'print(1)'", "python -c"),
        ("", "obrigatório"),
    ]:
        with pytest.raises(ToolError) as blocked:
            await tool.run({"command": command})
        assert fragment in str(blocked.value), command
    assert runner.calls == []  # nada chegou ao runner

    # o que passa pela política
    for argv in (["git", "status", "--short"], ["ruff", "check", "."], ["uv", "run", "pytest"]):
        check_command_policy(argv)


@pytest.mark.asyncio
async def test_failure_and_timeout_are_reported_not_raised(tmp_path: Path) -> None:
    failed = await RunCommandTool(_FakeRunner(exit_code=1, stderr="E  assert 1 == 2"), tmp_path).run(
        {"command": "pytest -q"}
    )
    assert "failed (exit_code=1)" in failed and "stderr:" in failed
    timed_out = await RunCommandTool(
        _FakeRunner(exit_code=None, stderr="timeout após 1s", timed_out=True), tmp_path
    ).run({"command": "pytest -q"})
    assert "timeout (exit_code=None)" in timed_out


@pytest.mark.asyncio
async def test_output_is_truncated(tmp_path: Path) -> None:
    tool = RunCommandTool(_FakeRunner(stdout="x" * 5000), tmp_path, max_output_chars=400)
    output = await tool.run({"command": "ruff check ."})
    assert len(output) < 500 and "caracteres omitidos" in output


def test_sanitized_environment_drops_secret_like_names() -> None:
    env = sanitized_environment(
        {
            "PATH": "/bin",
            "HOME": "/home/x",
            "ANTHROPIC_API_KEY": "sk",
            "GITHUB_TOKEN": "gh",
            "WEBHOOK_SIGNING_SECRET": "s",
            "DB_PASSWORD": "p",
            "AWS_CREDENTIALS": "c",
            "PYTHONPATH": ".",
        }
    )
    assert env == {"PATH": "/bin", "HOME": "/home/x", "PYTHONPATH": "."}


@pytest.mark.asyncio
async def test_local_runner_hides_secrets_and_enforces_timeout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-teste-nao-vazar")
    (tmp_path / "leak.py").write_text(
        "import os; print(os.environ.get('ANTHROPIC_API_KEY', 'ausente'))", encoding="utf-8"
    )
    (tmp_path / "slow.py").write_text("import time; time.sleep(30)", encoding="utf-8")

    runner = LocalCommandRunner(timeout_seconds=2.0, sanitize_env=True)
    leak = await runner.run("python leak.py", tmp_path, 4000)
    assert leak["exit_code"] == 0 and leak["stdout"].strip() == "ausente"
    assert leak["timed_out"] is False

    plain = LocalCommandRunner()
    visible = await plain.run("python leak.py", tmp_path, 4000)
    assert visible["stdout"].strip() == "sk-teste-nao-vazar"  # comportamento legado dos validadores

    slow = await LocalCommandRunner(timeout_seconds=0.5).run("python slow.py", tmp_path, 4000)
    assert slow["timed_out"] is True and slow["exit_code"] is None
    assert "timeout" in slow["stderr"]


def test_build_agent_tools_offers_run_command_only_to_executor(tmp_path: Path) -> None:
    default = Settings(_env_file=None)
    assert "run_command" not in {t.name for t in build_agent_tools(default, str(tmp_path), role="executor")}

    enabled = Settings(_env_file=None, agent_tools_allow_commands=True)
    executor_tools = {t.name for t in build_agent_tools(enabled, str(tmp_path), role="executor")}
    assert "run_command" in executor_tools and "read_file" in executor_tools
    for role in ("planner", "judge"):
        assert "run_command" not in {t.name for t in build_agent_tools(enabled, str(tmp_path), role=role)}
    assert "run_command" in COMMAND_TOOL_GUIDANCE


@pytest.mark.skipif(sys.platform == "win32" and not os.environ.get("CI"), reason="apenas sanidade em CI")
def test_placeholder_for_ci() -> None:
    assert True
