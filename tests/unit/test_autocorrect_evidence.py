"""Achados da rodada real com escrita de arquivos (04/09/2026).

- Rodada 2 de autocorreção sem operações devolvia workspace vazio e apagava os
  arquivos aplicados na rodada 1; a entrega publicaria nada.
- pytest com exit 5 (nenhum teste coletado) contava como reprovação e
  disparava uma rodada de autocorreção só para o modelo dizer "está tudo lá".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.executor import LLMExecutor
from app.infrastructure.workspace_runtime import (
    PYTEST_NO_TESTS_COLLECTED,
    CommandObjectiveValidator,
)


def _round_one() -> dict:
    return {
        "applied_files": ["pkg/__init__.py", "pkg/core.py", "pyproject.toml"],
        "published_files": [
            {"path": "pkg/__init__.py", "content": ""},
            {"path": "pkg/core.py", "content": "v1"},
            {"path": "pyproject.toml", "content": "[project]"},
        ],
        "file_diffs": [{"path": "pkg/core.py", "diff": "+v1"}],
        "deleted_paths": [],
        "operation_history": [{"step": "create", "path": "pkg/core.py"}],
        "command_feedback": [{"name": "pytest", "passed": False}],
        "strategy": {"apply_files": True},
    }


def test_round_without_operations_keeps_earlier_files() -> None:
    empty_round = {
        "apply_files_enabled": True,
        "applied_files": [],
        "workspace_root": "/ws",
        "strategy": {"apply_files": True},
    }
    merged = LLMExecutor._merge_workspace_evidence(_round_one(), empty_round)

    assert merged["applied_files"] == ["pkg/__init__.py", "pkg/core.py", "pyproject.toml"]
    assert [f["path"] for f in merged["published_files"]] == [
        "pkg/__init__.py",
        "pkg/core.py",
        "pyproject.toml",
    ]
    assert [d["path"] for d in merged["file_diffs"]] == ["pkg/core.py"]
    assert merged["operation_history"] == [{"step": "create", "path": "pkg/core.py"}]
    # chaves da rodada atual prevalecem
    assert merged["workspace_root"] == "/ws"
    assert "command_feedback" not in merged


def test_later_round_overrides_content_and_honours_deletions() -> None:
    round_two = {
        "applied_files": ["pkg/core.py", "pkg/extra.py"],
        "published_files": [
            {"path": "pkg/core.py", "content": "v2"},
            {"path": "pkg/extra.py", "content": "x"},
        ],
        "file_diffs": [{"path": "pkg/core.py", "diff": "+v2"}],
        "deleted_paths": ["pyproject.toml"],
        "operation_history": [{"step": "replace", "path": "pkg/core.py"}],
        "command_feedback": [{"name": "pytest", "passed": True}],
    }
    merged = LLMExecutor._merge_workspace_evidence(_round_one(), round_two)

    published = {f["path"]: f["content"] for f in merged["published_files"]}
    assert published == {"pkg/__init__.py": "", "pkg/core.py": "v2", "pkg/extra.py": "x"}
    assert merged["deleted_paths"] == ["pyproject.toml"]
    assert merged["applied_files"] == [
        "pkg/__init__.py",
        "pkg/core.py",
        "pyproject.toml",
        "pkg/extra.py",
    ]
    assert [d["diff"] for d in merged["file_diffs"]] == ["+v2"]
    assert len(merged["operation_history"]) == 2
    assert merged["command_feedback"] == [{"name": "pytest", "passed": True}]


def test_first_round_is_returned_as_is_and_non_dict_is_tolerated() -> None:
    first = _round_one()
    assert LLMExecutor._merge_workspace_evidence(None, first) == first
    assert LLMExecutor._merge_workspace_evidence(first, None)["applied_files"] == first["applied_files"]


class _Runner:
    def __init__(self, exit_code: int, stdout: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout

    async def run(self, command: str, workspace_root: Path, output_limit: int) -> dict:
        return {
            "command": command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": "",
        }


@pytest.mark.asyncio
async def test_pytest_without_collected_tests_is_no_signal_not_failure(tmp_path) -> None:
    validator = CommandObjectiveValidator(
        name="pytest",
        command="python -m pytest -q",
        workspace_root=str(tmp_path),
        command_runner=_Runner(PYTEST_NO_TESTS_COLLECTED, "no tests ran"),
    )
    signal = await validator.execute()
    assert signal.passed is None
    assert signal.exit_code == PYTEST_NO_TESTS_COLLECTED
    assert "nenhum teste coletado" in signal.details


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["ruff", "mypy"])
async def test_exit_five_stays_a_failure_for_other_validators(tmp_path, name) -> None:
    validator = CommandObjectiveValidator(
        name=name,
        command=f"{name} .",
        workspace_root=str(tmp_path),
        command_runner=_Runner(5, "boom"),
    )
    signal = await validator.execute()
    assert signal.passed is False


@pytest.mark.asyncio
async def test_pytest_real_failure_is_still_a_failure(tmp_path) -> None:
    validator = CommandObjectiveValidator(
        name="pytest",
        command="python -m pytest -q",
        workspace_root=str(tmp_path),
        command_runner=_Runner(1, "1 failed"),
    )
    assert (await validator.execute()).passed is False


def test_net_change_type_is_relative_to_task_start() -> None:
    """Achado dos evals (04/09/2026): criado na rodada 1 e editado na rodada 2
    aparecia como 'modified' e reprovava file_created."""
    round_one = {
        "applied_files": ["tests/test_core.py", "old.py"],
        "published_files": [{"path": "tests/test_core.py", "content": "v1"}],
        "file_diffs": [
            {"path": "tests/test_core.py", "change_type": "created", "changed": True},
            {"path": "old.py", "change_type": "deleted", "changed": True},
        ],
        "deleted_paths": ["old.py"],
    }
    round_two = {
        "applied_files": ["tests/test_core.py", "old.py"],
        "published_files": [
            {"path": "tests/test_core.py", "content": "v2"},
            {"path": "old.py", "content": "de volta"},
        ],
        "file_diffs": [
            {"path": "tests/test_core.py", "change_type": "modified", "changed": True},
            {"path": "old.py", "change_type": "created", "changed": True},
        ],
        "deleted_paths": [],
    }
    merged = LLMExecutor._merge_workspace_evidence(round_one, round_two)
    kinds = {d["path"]: d["change_type"] for d in merged["file_diffs"]}
    assert kinds == {"tests/test_core.py": "created", "old.py": "modified"}
    assert {p["path"]: p["content"] for p in merged["published_files"]} == {"tests/test_core.py": "v2", "old.py": "de volta"}
    assert merged["deleted_paths"] == []

    # criado e removido na tarefa: nunca existiu — sai de tudo
    round_three = {"applied_files": ["tests/test_core.py"], "published_files": [], "file_diffs": [], "deleted_paths": ["tests/test_core.py"]}
    gone = LLMExecutor._merge_workspace_evidence(
        {"published_files": [{"path": "tests/test_core.py", "content": "v1"}], "file_diffs": [{"path": "tests/test_core.py", "change_type": "created", "changed": True}], "deleted_paths": []},
        round_three,
    )
    assert gone["file_diffs"] == [] and gone["published_files"] == [] and gone["deleted_paths"] == []
