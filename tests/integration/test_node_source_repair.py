"""Real Node checks and file edits; only model responses are deterministic.

This reproduces the controller's missing-source failure, not model quality.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from app.agents.executor import LLMExecutor
from app.agents.tools import build_workspace_tools
from app.infrastructure.command_policy import CommandPolicy
from app.infrastructure.workspace_runtime import (
    CommandObjectiveValidator,
    LocalCommandRunner,
    LocalWorkspaceRuntime,
)
from app.models.task import AgentTask, Capability
from app.providers.base import CompletionResult, Usage


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
@pytest.mark.asyncio
async def test_node_repair_receives_current_source_after_failed_test(tmp_path):
    fixture = Path(__file__).resolve().parents[2] / "benchmarks/factory/fixtures/node"
    shutil.copytree(fixture, tmp_path, dirs_exist_ok=True)
    target = tmp_path / "tests/catalog.test.cjs"
    original = target.read_text()
    broken = "expect(retail(10)).toBe(12);"
    correct = "assert.equal(retail(10), 12);"

    class SourceDependentRouter:
        def __init__(self):
            self.requests = []

        async def complete(self, tier, request):
            self.requests.append(request)
            reads = [
                result.content
                for message in request.messages
                for result in message.tool_results
                if result.name == "read_file" and not result.is_error
            ]
            if len(self.requests) == 1:
                search, replacement = correct, broken
            else:
                # Without a fresh source read, the only new evidence is TAP
                # diagnostics. A repair must receive the exact changed source.
                search = (
                    broken
                    if any(broken in read for read in reads)
                    else "not ok 1 - prices include tax"
                )
                replacement = correct
            return CompletionResult(
                text="",
                parsed={
                    "summary": "repair test",
                    "operations": [
                        {
                            "op": "replace",
                            "path": "tests/catalog.test.cjs",
                            "search": search,
                            "replace": replacement,
                        }
                    ],
                },
                model="scripted",
                provider="test",
                usage=Usage(),
                cost_usd=0,
                latency_ms=0,
            )

    validator = CommandObjectiveValidator(
        name="node_tests",
        command="node --test tests/catalog.test.cjs",
        workspace_root=str(tmp_path),
        command_runner=LocalCommandRunner(
            CommandPolicy(allowed_executables={"node"}),
            timeout_seconds=10,
            sanitize_env=True,
        ),
    )
    router = SourceDependentRouter()
    executor = LLMExecutor(
        router,
        "test_executor",
        max_autocorrect_rounds=1,
        workspace_runtime=LocalWorkspaceRuntime(
            str(tmp_path),
            apply_files_enabled=True,
            command_feedback_runners=[validator],
        ),
        tools=build_workspace_tools(str(tmp_path)),
    )
    task = AgentTask(
        title="Fix Node tests",
        description="Preserve node:assert tests",
        capability=Capability.TESTING,
        acceptance_criteria=["Tests pass"],
    )
    outcome = await executor.execute(
        task,
        {
            "repository_grounding": {
                "evidence": [
                    {
                        "id": "E1",
                        "path": "tests/catalog.test.cjs",
                        "line_start": 1,
                        "line_end": 7,
                        "excerpt": original,
                    }
                ],
            }
        },
    )

    assert target.read_text() == original
    workspace = outcome["result"]["workspace"]
    assert workspace["autocorrect"]["iterations"][0]["failed_checks"] == ["node_tests"]
    assert workspace["autocorrect"]["stopped_reason"] == "checks_passed_or_skipped"
    # Independent execution verifies the artifact, not the model summary.
    check = subprocess.run(
        [shutil.which("node"), "--test", "tests/catalog.test.cjs"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    assert len(router.requests) == 2
