import subprocess
import shlex
import sys
from pathlib import Path

import pytest

from app.agents.executor import ExecutionStrategy, LLMExecutor
from app.agents.validation import (
    ObjectiveValidationPipeline,
    format_validation_feedback,
)
from app.api.container import (
    build_execution_strategies,
    build_objective_validation_pipeline,
)
from app.infrastructure.workspace_runtime import (
    CommandPolicy,
    CommandObjectiveValidator,
    DockerSandboxCommandRunner,
    LocalWorkspaceRuntime,
)
from app.models.task import AgentTask, Capability, TaskAttempt
from app.infrastructure.settings import Settings
from app.providers.base import CompletionResult, Usage


PYTHON_COMMAND = shlex.quote(sys.executable)


class StaticRouter:
    def __init__(self, payload):
        self._payload = payload

    async def complete(self, tier, request):
        return CompletionResult(
            text="ok",
            parsed=self._payload,
            model="fake",
            provider="fake",
            usage=Usage(),
            cost_usd=0.0,
            latency_ms=0.0,
        )


class CapturingRouter(StaticRouter):
    def __init__(self, payload):
        super().__init__(payload)
        self.last_request = None

    async def complete(self, tier, request):
        self.last_request = request
        return await super().complete(tier, request)


class SequentialRouter:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.requests = []

    async def complete(self, tier, request):
        self.requests.append(request)
        payload = self._payloads.pop(0)
        return CompletionResult(
            text="ok",
            parsed=payload["parsed"],
            model=payload.get("model", "fake"),
            provider="fake",
            usage=Usage(input_tokens=payload.get("tokens", 0), output_tokens=0),
            cost_usd=payload.get("cost_usd", 0.0),
            latency_ms=0.0,
        )


def test_command_policy_blocks_unknown_executables():
    policy = CommandPolicy()
    with pytest.raises(ValueError, match="não permitido"):
        policy.parse("curl https://example.com")


def test_docker_sandbox_is_hardened_and_networkless(tmp_path: Path):
    runner = DockerSandboxCommandRunner(image="forgehand-sandbox:test")
    argv = runner.build_argv("pytest -q", tmp_path)

    assert argv[:3] == ["docker", "run", "--rm"]
    assert argv[argv.index("--network") + 1] == "none"
    assert "no-new-privileges" in argv
    assert "ALL" in argv
    assert f"{tmp_path}:/workspace:rw" in argv


@pytest.mark.asyncio
async def test_local_workspace_runtime_applies_artifacts(tmp_path: Path):
    runtime = LocalWorkspaceRuntime(str(tmp_path), apply_files_enabled=True)
    task = AgentTask(
        title="backend",
        description="criar arquivo",
        capability=Capability.BACKEND,
        acceptance_criteria=["arquivo existe"],
    )

    metadata = await runtime.apply(
        task,
        {
            "files": [
                {"path": "generated/example.py", "content": "print('ok')\n"},
            ]
        },
    )

    assert (tmp_path / "generated" / "example.py").read_text(
        encoding="utf-8"
    ) == "print('ok')\n"
    assert metadata["workspace"]["applied_files"] == ["generated/example.py"]


@pytest.mark.asyncio
async def test_local_workspace_runtime_blocks_path_traversal(tmp_path: Path):
    runtime = LocalWorkspaceRuntime(str(tmp_path), apply_files_enabled=True)
    task = AgentTask(
        title="backend",
        description="criar arquivo",
        capability=Capability.BACKEND,
        acceptance_criteria=["arquivo existe"],
    )

    with pytest.raises(ValueError, match="fora do workspace"):
        await runtime.apply(
            task,
            {"files": [{"path": "../escape.py", "content": "x = 1\n"}]},
        )


@pytest.mark.asyncio
async def test_command_objective_validator_runs_shell_command(tmp_path: Path):
    (tmp_path / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    validator = CommandObjectiveValidator(
        name="ruff",
        command=f"{PYTHON_COMMAND} -c \"print('lint ok')\"",
        workspace_root=str(tmp_path),
        file_suffixes={".py"},
    )
    task = AgentTask(
        title="lint",
        description="validar",
        capability=Capability.BACKEND,
        acceptance_criteria=["passa no lint"],
        result={"workspace": {"applied_files": ["ok.py"]}},
    )

    signal = await validator.validate(task)

    assert signal.passed is True
    assert "lint ok" in signal.details
    assert signal.command == f"{PYTHON_COMMAND} -c \"print('lint ok')\""
    assert signal.exit_code == 0
    assert "lint ok" in signal.stdout


@pytest.mark.asyncio
async def test_local_workspace_runtime_collects_command_feedback(tmp_path: Path):
    runtime = LocalWorkspaceRuntime(
        str(tmp_path),
        apply_files_enabled=True,
        command_feedback_runners=[
            CommandObjectiveValidator(
                name="ruff",
                command=f"{PYTHON_COMMAND} -c \"print('lint ok')\"",
                workspace_root=str(tmp_path),
                file_suffixes={".py"},
            )
        ],
    )
    task = AgentTask(
        title="backend",
        description="criar arquivo",
        capability=Capability.BACKEND,
        acceptance_criteria=["arquivo existe"],
    )

    metadata = await runtime.apply(
        task,
        {
            "files": [
                {"path": "generated/example.py", "content": "print('ok')\n"},
            ]
        },
    )

    feedback = metadata["workspace"]["command_feedback"]
    assert feedback[0]["name"] == "ruff"
    assert feedback[0]["passed"] is True
    assert feedback[0]["command"] == f"{PYTHON_COMMAND} -c \"print('lint ok')\""
    assert feedback[0]["exit_code"] == 0
    assert "lint ok" in feedback[0]["stdout"]
    assert metadata["workspace"]["operation_history"][-1]["step"] == "command_feedback"
    assert metadata["workspace"]["command_executions"][0]["name"] == "ruff"
    assert metadata["workspace"]["command_executions"][0]["exit_code"] == 0


@pytest.mark.asyncio
async def test_local_workspace_runtime_collects_file_diffs(tmp_path: Path):
    target = tmp_path / "generated" / "example.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('before')\n", encoding="utf-8")
    runtime = LocalWorkspaceRuntime(str(tmp_path), apply_files_enabled=True)
    task = AgentTask(
        title="backend",
        description="atualizar arquivo",
        capability=Capability.BACKEND,
        acceptance_criteria=["arquivo atualizado"],
    )

    metadata = await runtime.apply(
        task,
        {
            "files": [
                {"path": "generated/example.py", "content": "print('after')\n"},
            ]
        },
    )

    diff = metadata["workspace"]["file_diffs"][0]
    assert diff["path"] == "generated/example.py"
    assert diff["change_type"] == "modified"
    assert "-print('before')" in diff["diff"]
    assert "+print('after')" in diff["diff"]
    assert metadata["workspace"]["operation_history"][0]["step"] == "apply_file"


@pytest.mark.asyncio
async def test_local_workspace_runtime_collects_git_snapshot(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tests"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    tracked = tmp_path / "tracked.py"
    tracked.write_text("print('before')\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "tracked.py"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    runtime = LocalWorkspaceRuntime(str(tmp_path), apply_files_enabled=True)
    task = AgentTask(
        title="backend",
        description="alterar arquivo rastreado",
        capability=Capability.BACKEND,
        acceptance_criteria=["arquivo atualizado"],
    )

    metadata = await runtime.apply(
        task,
        {"files": [{"path": "tracked.py", "content": "print('after')\n"}]},
    )

    git_snapshot = metadata["workspace"]["git_snapshot"]
    assert git_snapshot is not None
    assert "tracked.py" in git_snapshot["status"]
    assert "-print('before')" in git_snapshot["diff"]
    assert "+print('after')" in git_snapshot["diff"]
    assert metadata["workspace"]["operation_history"][-1]["step"] == "git_snapshot"


@pytest.mark.asyncio
async def test_command_objective_validator_reuses_cached_feedback(tmp_path: Path):
    validator = CommandObjectiveValidator(
        name="ruff",
        command=f'{PYTHON_COMMAND} -c "import sys; sys.exit(1)"',
        workspace_root=str(tmp_path),
        file_suffixes={".py"},
    )
    task = AgentTask(
        title="backend",
        description="validar",
        capability=Capability.BACKEND,
        acceptance_criteria=["passa no lint"],
        result={
            "workspace": {
                "applied_files": ["ok.py"],
                "command_feedback": [
                    {
                        "name": "ruff",
                        "passed": True,
                        "details": "cached ok",
                        "command": "ruff check .",
                        "exit_code": 0,
                        "stdout": "cached ok",
                        "stderr": "",
                    }
                ],
            }
        },
    )

    signal = await validator.validate(task)

    assert signal.passed is True
    assert signal.details == "cached ok"


def test_format_validation_feedback_includes_command_and_outputs():
    feedback = format_validation_feedback(
        [
            {
                "name": "pytest",
                "passed": False,
                "details": "2 failed",
                "command": "uv run pytest tests/unit/test_example.py",
                "exit_code": 1,
                "stdout": "FAILED tests/unit/test_example.py::test_case",
                "stderr": "AssertionError: boom",
            }
        ]
    )

    assert "pytest: failed (exit_code=1)" in feedback
    assert "command: uv run pytest tests/unit/test_example.py" in feedback
    assert "details: 2 failed" in feedback
    assert "stdout: FAILED tests/unit/test_example.py::test_case" in feedback
    assert "stderr: AssertionError: boom" in feedback


@pytest.mark.asyncio
async def test_llm_executor_writes_files_when_workspace_runtime_enabled(tmp_path: Path):
    executor = LLMExecutor(
        StaticRouter(
            {
                "summary": "arquivo criado",
                "files": [{"path": "generated/example.py", "content": "print('ok')\n"}],
                "notes": [],
                "citations": [],
            }
        ),
        agent_name="backend_executor",
        workspace_runtime=LocalWorkspaceRuntime(
            str(tmp_path),
            apply_files_enabled=True,
        ),
    )
    task = AgentTask(
        title="backend",
        description="criar arquivo",
        capability=Capability.BACKEND,
        acceptance_criteria=["arquivo existe"],
    )

    outcome = await executor.execute(task, {})

    assert outcome["result"]["workspace"]["applied_files"] == ["generated/example.py"]
    assert (tmp_path / "generated" / "example.py").exists()
    assert outcome["result"]["workspace"]["file_diffs"][0]["change_type"] == "created"


@pytest.mark.asyncio
async def test_llm_executor_backfills_grounded_citations_when_missing():
    executor = LLMExecutor(
        StaticRouter(
            {
                "summary": "arquivo criado",
                "files": [{"path": "generated/example.py", "content": "print('ok')\n"}],
                "notes": [],
                "citations": [],
            }
        ),
        agent_name="backend_executor",
    )
    task = AgentTask(
        title="backend",
        description="criar arquivo",
        capability=Capability.BACKEND,
        acceptance_criteria=["arquivo existe"],
        evidence_ids=["E1"],
    )

    outcome = await executor.execute(
        task,
        {
            "task_evidence_ids": ["E1"],
            "repository_grounding": {
                "repo_root": "/repo",
                "require_citations": True,
                "evidence": [
                    {
                        "id": "E1",
                        "path": "app/main.py",
                        "line_start": 1,
                        "line_end": 3,
                        "excerpt": "from fastapi import FastAPI",
                    }
                ],
            },
        },
    )

    assert outcome["result"]["citations"] == ["E1"]


@pytest.mark.asyncio
async def test_llm_executor_overrides_out_of_scope_grounded_citations():
    executor = LLMExecutor(
        StaticRouter(
            {
                "summary": "arquivo criado",
                "files": [{"path": "generated/example.py", "content": "print('ok')\n"}],
                "notes": [],
                "citations": ["WRONG-ID"],
            }
        ),
        agent_name="backend_executor",
    )
    task = AgentTask(
        title="backend",
        description="criar arquivo",
        capability=Capability.BACKEND,
        acceptance_criteria=["arquivo existe"],
        evidence_ids=["E1"],
    )

    outcome = await executor.execute(
        task,
        {
            "task_evidence_ids": ["E1"],
            "repository_grounding": {
                "repo_root": "/repo",
                "require_citations": True,
                "evidence": [
                    {
                        "id": "E1",
                        "path": "app/main.py",
                        "line_start": 1,
                        "line_end": 3,
                        "excerpt": "from fastapi import FastAPI",
                    }
                ],
            },
        },
    )

    assert outcome["result"]["citations"] == ["E1"]


@pytest.mark.asyncio
async def test_llm_executor_includes_previous_command_feedback_in_retry_prompt():
    router = CapturingRouter(
        {
            "summary": "corrigido",
            "files": [],
            "notes": [],
            "citations": [],
        }
    )
    executor = LLMExecutor(
        router,
        agent_name="backend_executor",
    )
    task = AgentTask(
        title="backend",
        description="corrigir testes",
        capability=Capability.BACKEND,
        acceptance_criteria=["testes passam"],
        result={
            "workspace": {
                "applied_files": ["generated/example.py"],
                "command_feedback": [
                    {
                        "name": "pytest",
                        "passed": False,
                        "details": "2 failed",
                        "command": "uv run pytest",
                        "exit_code": 1,
                        "stderr": "AssertionError: boom",
                    }
                ],
            }
        },
    )
    task.attempts.append(
        TaskAttempt(
            attempt_number=1,
            agent_name="backend_executor",
            model="fake",
            started_at=task.updated_at,
        )
    )

    await executor.execute(task, {})

    assert router.last_request is not None
    assert (
        "Feedback operacional da tentativa anterior"
        in router.last_request.messages[0].content
    )
    assert "pytest: failed (exit_code=1)" in router.last_request.messages[0].content
    assert "command: uv run pytest" in router.last_request.messages[0].content
    assert "stderr: AssertionError: boom" in router.last_request.messages[0].content


@pytest.mark.asyncio
async def test_llm_executor_autocorrects_within_same_attempt(tmp_path: Path):
    validator = CommandObjectiveValidator(
        name="pytest",
        command=(
            f'{PYTHON_COMMAND} -c "from pathlib import Path; import sys; '
            "sys.exit(0 if 'fixed' in Path('generated/example.py').read_text() else 1)\""
        ),
        workspace_root=str(tmp_path),
        file_suffixes={".py"},
    )
    executor = LLMExecutor(
        SequentialRouter(
            [
                {
                    "parsed": {
                        "summary": "primeira tentativa",
                        "files": [
                            {
                                "path": "generated/example.py",
                                "content": "print('broken')\n",
                            }
                        ],
                        "notes": [],
                        "citations": [],
                    },
                    "tokens": 11,
                    "cost_usd": 0.2,
                },
                {
                    "parsed": {
                        "summary": "segunda tentativa",
                        "files": [
                            {
                                "path": "generated/example.py",
                                "content": "print('fixed')\n",
                            }
                        ],
                        "notes": [],
                        "citations": [],
                    },
                    "tokens": 13,
                    "cost_usd": 0.3,
                },
            ]
        ),
        agent_name="backend_executor",
        workspace_runtime=LocalWorkspaceRuntime(
            str(tmp_path),
            apply_files_enabled=True,
            command_feedback_runners=[validator],
        ),
        max_autocorrect_rounds=1,
    )
    task = AgentTask(
        title="backend",
        description="corrigir arquivo até passar no check",
        capability=Capability.BACKEND,
        acceptance_criteria=["check passa"],
    )

    outcome = await executor.execute(task, {})

    assert outcome["tokens"] == 24
    assert outcome["cost_usd"] == 0.5
    assert (tmp_path / "generated" / "example.py").read_text(
        encoding="utf-8"
    ) == "print('fixed')\n"
    autocorrect = outcome["result"]["workspace"]["autocorrect"]
    assert autocorrect["total_iterations"] == 2
    assert autocorrect["stopped_reason"] == "checks_passed_or_skipped"
    assert autocorrect["iterations"][0]["failed_checks"] == ["pytest"]
    assert autocorrect["iterations"][1]["failed_checks"] == []


@pytest.mark.asyncio
async def test_llm_executor_includes_internal_feedback_on_autocorrect_retry(
    tmp_path: Path,
):
    validator = CommandObjectiveValidator(
        name="pytest",
        command=f'{PYTHON_COMMAND} -c "import sys; sys.exit(1)"',
        workspace_root=str(tmp_path),
        file_suffixes={".py"},
    )
    router = SequentialRouter(
        [
            {
                "parsed": {
                    "summary": "primeira tentativa",
                    "files": [
                        {"path": "generated/example.py", "content": "print('broken')\n"}
                    ],
                    "notes": [],
                    "citations": [],
                }
            },
            {
                "parsed": {
                    "summary": "segunda tentativa",
                    "files": [
                        {
                            "path": "generated/example.py",
                            "content": "print('broken again')\n",
                        }
                    ],
                    "notes": [],
                    "citations": [],
                }
            },
        ]
    )
    executor = LLMExecutor(
        router,
        agent_name="backend_executor",
        workspace_runtime=LocalWorkspaceRuntime(
            str(tmp_path),
            apply_files_enabled=True,
            command_feedback_runners=[validator],
        ),
        max_autocorrect_rounds=1,
    )
    task = AgentTask(
        title="backend",
        description="tentar corrigir",
        capability=Capability.BACKEND,
        acceptance_criteria=["check passa"],
    )

    await executor.execute(task, {})

    assert len(router.requests) == 2
    assert (
        "Feedback operacional da iteração interna anterior"
        in router.requests[1].messages[0].content
    )
    assert "pytest: failed (exit_code=1)" in router.requests[1].messages[0].content
    assert f"command: {PYTHON_COMMAND} -c" in router.requests[1].messages[0].content


@pytest.mark.asyncio
async def test_runtime_uses_pipeline_to_filter_validators_by_capability(tmp_path: Path):
    pipeline = ObjectiveValidationPipeline(
        [
            CommandObjectiveValidator(
                name="pytest",
                command=f"{PYTHON_COMMAND} -c \"print('pytest ok')\"",
                workspace_root=str(tmp_path),
                file_suffixes={".py"},
            ),
            CommandObjectiveValidator(
                name="ruff",
                command=f"{PYTHON_COMMAND} -c \"print('ruff ok')\"",
                workspace_root=str(tmp_path),
                file_suffixes={".py"},
            ),
        ],
        capability_pipelines={
            Capability.FRONTEND: ["pytest"],
            Capability.BACKEND: ["ruff", "pytest"],
        },
    )
    runtime = LocalWorkspaceRuntime(
        str(tmp_path),
        apply_files_enabled=True,
        validation_pipeline=pipeline,
    )
    task = AgentTask(
        title="frontend",
        description="gerar arquivo",
        capability=Capability.FRONTEND,
        acceptance_criteria=["arquivo existe"],
    )

    metadata = await runtime.apply(
        task,
        {"files": [{"path": "generated/example.py", "content": "print('ok')\n"}]},
    )

    assert [item["name"] for item in metadata["workspace"]["command_feedback"]] == [
        "pytest"
    ]


def test_build_objective_validation_pipeline_respects_settings_order(tmp_path: Path):
    settings = Settings(
        executor_workspace_root=str(tmp_path),
        pytest_validation_command=f"{PYTHON_COMMAND} -c \"print('pytest ok')\"",
        ruff_validation_command=f"{PYTHON_COMMAND} -c \"print('ruff ok')\"",
        objective_validation_pipelines_json=(
            '{"backend":["pytest","ruff"],"documentation":[]}'
        ),
    )

    pipeline = build_objective_validation_pipeline(
        settings,
        [
            CommandObjectiveValidator(
                name="pytest",
                command=settings.pytest_validation_command or "",
                workspace_root=str(tmp_path),
            ),
            CommandObjectiveValidator(
                name="ruff",
                command=settings.ruff_validation_command or "",
                workspace_root=str(tmp_path),
            ),
        ],
    )

    backend_task = AgentTask(
        title="backend",
        description="x",
        capability=Capability.BACKEND,
        acceptance_criteria=["y"],
    )
    docs_task = AgentTask(
        title="docs",
        description="x",
        capability=Capability.DOCUMENTATION,
        acceptance_criteria=["y"],
    )

    assert [
        validator.name for validator in pipeline.validators_for_task(backend_task)
    ] == [
        "pytest",
        "ruff",
    ]
    assert pipeline.validators_for_task(docs_task) == []


@pytest.mark.asyncio
async def test_runtime_execution_strategy_can_skip_apply_and_checks(tmp_path: Path):
    runtime = LocalWorkspaceRuntime(
        str(tmp_path),
        apply_files_enabled=True,
        validation_pipeline=ObjectiveValidationPipeline([]),
    )
    task = AgentTask(
        title="review",
        description="produzir revisão sem tocar no workspace",
        capability=Capability.REVIEW,
        acceptance_criteria=["resultado entregue"],
    )

    metadata = await runtime.apply(
        task,
        {"files": [{"path": "generated/example.py", "content": "print('noop')\n"}]},
        ExecutionStrategy(
            apply_files=False,
            run_objective_validation=False,
            allow_autocorrect=False,
        ),
    )

    assert metadata["workspace"]["apply_files_enabled"] is False
    assert metadata["workspace"]["applied_files"] == []
    assert not (tmp_path / "generated" / "example.py").exists()
    assert metadata["workspace"]["strategy"]["apply_files"] is False


@pytest.mark.asyncio
async def test_runtime_execution_strategy_can_apply_without_objective_validation(
    tmp_path: Path,
):
    runtime = LocalWorkspaceRuntime(
        str(tmp_path),
        apply_files_enabled=True,
        validation_pipeline=ObjectiveValidationPipeline(
            [
                CommandObjectiveValidator(
                    name="pytest",
                    command=f"{PYTHON_COMMAND} -c \"print('pytest ok')\"",
                    workspace_root=str(tmp_path),
                    file_suffixes={".py"},
                )
            ]
        ),
    )
    task = AgentTask(
        title="docs",
        description="gerar documentação",
        capability=Capability.DOCUMENTATION,
        acceptance_criteria=["arquivo existe"],
    )

    metadata = await runtime.apply(
        task,
        {"files": [{"path": "docs/output.md", "content": "# ok\n"}]},
        ExecutionStrategy(
            apply_files=True,
            run_objective_validation=False,
            allow_autocorrect=False,
        ),
    )

    assert (tmp_path / "docs" / "output.md").exists()
    assert metadata["workspace"]["command_feedback"] == []
    assert metadata["workspace"]["git_snapshot"] is None


def test_build_execution_strategies_respects_settings():
    settings = Settings(
        executor_strategies_json=(
            '{"review":{"apply_files":false,"run_objective_validation":false,"allow_autocorrect":false},'
            '"backend":{"apply_files":true,"run_objective_validation":true,"allow_autocorrect":true}}'
        )
    )

    strategies = build_execution_strategies(settings)

    assert strategies[Capability.REVIEW].apply_files is False
    assert strategies[Capability.REVIEW].run_objective_validation is False
    assert strategies[Capability.BACKEND].allow_autocorrect is True
