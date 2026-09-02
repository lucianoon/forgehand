"""Operações de arquivo (create/replace/delete) no executor e no workspace
runtime; conteúdo final na publicação de PR; veto do judge em falha de apply."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.executor import ExecutionOutput, LLMExecutor
from app.agents.criteria import no_existing_file_modified
from app.agents.judge import LLMJudge
from app.infrastructure.scm import collect_publishable_changes
from app.infrastructure.repository_grounding import RepositoryGroundingCollector
from app.infrastructure.workspace_runtime import (
    LocalWorkspaceRuntime,
    OperationApplyError,
    find_replace_span,
    normalize_operations,
)
from app.models.task import AgentTask, Capability
from app.providers.base import CompletionResult, Usage


def _task(**overrides) -> AgentTask:
    base = dict(
        title="backend",
        description="editar arquivo",
        capability=Capability.BACKEND,
        acceptance_criteria=["arquivo atualizado"],
    )
    base.update(overrides)
    return AgentTask(**base)


def _runtime(root: Path) -> LocalWorkspaceRuntime:
    return LocalWorkspaceRuntime(str(root), apply_files_enabled=True)


# --------------------------------------------------------------------------
# Schema do executor
# --------------------------------------------------------------------------


def test_execution_output_schema_exposes_operations_not_legacy_files():
    schema = ExecutionOutput.model_json_schema()
    assert "operations" in schema["properties"]
    assert "files" not in schema["properties"]
    # discriminador por `op`: o modelo escolhe entre create/replace/delete
    dumped = json.dumps(schema)
    for op in ("create", "replace", "delete"):
        assert f'"{op}"' in dumped


def test_execution_output_still_accepts_legacy_files_payload():
    output = ExecutionOutput.model_validate(
        {"summary": "ok", "files": [{"path": "a.py", "content": "x"}]}
    )
    assert output.files[0].path == "a.py"
    assert output.operations == []


def test_normalize_operations_merges_legacy_and_new():
    ops = normalize_operations(
        {
            "files": [{"path": "a.py", "content": "x"}],
            "operations": [{"op": "delete", "path": "b.py"}],
        }
    )
    assert ops == [
        {"op": "create", "path": "a.py", "content": "x"},
        {"op": "delete", "path": "b.py"},
    ]


# --------------------------------------------------------------------------
# find_replace_span
# --------------------------------------------------------------------------


def test_find_replace_span_exact_unique():
    assert find_replace_span("a = 1\nb = 2\n", "b = 2", None) == (6, 11)


def test_find_replace_span_rejects_missing_and_ambiguous():
    with pytest.raises(OperationApplyError, match="não encontrado"):
        find_replace_span("a = 1\n", "z = 9", None)
    with pytest.raises(OperationApplyError, match="aparece 2 vezes"):
        find_replace_span("x\nx\n", "x", None)


def test_find_replace_span_honours_occurrence():
    assert find_replace_span("x\nx\n", "x", 2) == (2, 3)
    with pytest.raises(OperationApplyError, match="apenas 2"):
        find_replace_span("x\nx\n", "x", 3)


def test_find_replace_span_tolerates_trailing_whitespace_and_crlf():
    text = "def f():   \n    return 1\n"
    search = "def f():\r\n    return 1"
    start, end = find_replace_span(text, search, None)
    assert text[start:end] == "def f():   \n    return 1"


# --------------------------------------------------------------------------
# Workspace runtime
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_applies_replace_and_records_operation(tmp_path: Path):
    target = tmp_path / "app" / "svc.py"
    target.parent.mkdir(parents=True)
    target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    metadata = await _runtime(tmp_path).apply(
        _task(),
        {
            "operations": [
                {
                    "op": "replace",
                    "path": "app/svc.py",
                    "search": "    return a - b\n",
                    "replace": "    return a + b\n",
                }
            ]
        },
    )

    assert target.read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    workspace = metadata["workspace"]
    assert workspace["applied_files"] == ["app/svc.py"]
    assert workspace["apply_errors"] == []
    diff = workspace["file_diffs"][0]
    assert diff["operation"] == "replace"
    assert diff["change_type"] == "modified"
    assert "-    return a - b" in diff["diff"]
    assert "+    return a + b" in diff["diff"]
    assert workspace["published_files"] == [
        {"path": "app/svc.py", "content": "def add(a, b):\n    return a + b\n"}
    ]
    assert workspace["deleted_paths"] == []


@pytest.mark.asyncio
async def test_runtime_reports_failed_replace_as_apply_signal(tmp_path: Path):
    target = tmp_path / "app" / "svc.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", encoding="utf-8")

    metadata = await _runtime(tmp_path).apply(
        _task(),
        {
            "operations": [
                {
                    "op": "replace",
                    "path": "app/svc.py",
                    "search": "y = 2",
                    "replace": "",
                },
                {"op": "create", "path": "app/new.py", "content": "ok\n"},
            ]
        },
    )

    workspace = metadata["workspace"]
    # a operação inválida não derruba as demais
    assert target.read_text(encoding="utf-8") == "x = 1\n"
    assert (tmp_path / "app" / "new.py").exists()
    assert workspace["applied_files"] == ["app/new.py"]
    assert workspace["apply_errors"][0]["operation"] == "replace"
    assert "não encontrado" in workspace["apply_errors"][0]["error"]
    apply_signal = workspace["command_feedback"][0]
    assert apply_signal["name"] == "apply"
    assert apply_signal["passed"] is False
    assert "replace app/svc.py" in apply_signal["details"]
    failed_step = workspace["operation_history"][0]
    assert failed_step["applied"] is False


@pytest.mark.asyncio
async def test_runtime_delete_and_legacy_files(tmp_path: Path):
    (tmp_path / "old.py").write_text("bye\n", encoding="utf-8")

    metadata = await _runtime(tmp_path).apply(
        _task(),
        {
            "files": [{"path": "gen/example.py", "content": "print('ok')\n"}],
            "operations": [{"op": "delete", "path": "old.py"}],
        },
    )

    workspace = metadata["workspace"]
    assert not (tmp_path / "old.py").exists()
    assert (tmp_path / "gen" / "example.py").exists()
    assert workspace["applied_files"] == ["gen/example.py", "old.py"]
    by_path = {item["path"]: item for item in workspace["file_diffs"]}
    assert by_path["gen/example.py"]["operation"] == "create"
    assert by_path["gen/example.py"]["change_type"] == "created"
    assert by_path["old.py"]["operation"] == "delete"
    assert by_path["old.py"]["change_type"] == "deleted"
    assert "-bye" in by_path["old.py"]["diff"]
    assert workspace["deleted_paths"] == ["old.py"]
    assert workspace["published_files"] == [
        {"path": "gen/example.py", "content": "print('ok')\n"}
    ]


@pytest.mark.asyncio
async def test_runtime_replace_on_missing_file_is_apply_error(tmp_path: Path):
    metadata = await _runtime(tmp_path).apply(
        _task(),
        {
            "operations": [
                {"op": "replace", "path": "ghost.py", "search": "a", "replace": "b"}
            ]
        },
    )
    assert metadata["workspace"]["applied_files"] == []
    assert "use op=create" in metadata["workspace"]["apply_errors"][0]["error"]


@pytest.mark.asyncio
async def test_runtime_still_blocks_path_traversal_for_operations(tmp_path: Path):
    with pytest.raises(ValueError, match="fora do workspace"):
        await _runtime(tmp_path).apply(
            _task(),
            {"operations": [{"op": "delete", "path": "../outside.py"}]},
        )


# --------------------------------------------------------------------------
# Executor: autocorrect com feedback de apply
# --------------------------------------------------------------------------


class SequentialRouter:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.requests = []

    async def complete(self, tier, request):
        self.requests.append(request)
        return CompletionResult(
            text="ok",
            parsed=self._payloads.pop(0),
            model="fake",
            provider="fake",
            usage=Usage(),
            cost_usd=0.0,
            latency_ms=0.0,
        )


@pytest.mark.asyncio
async def test_executor_retries_with_apply_feedback_and_succeeds(tmp_path: Path):
    target = tmp_path / "svc.py"
    target.write_text("value = 1\n", encoding="utf-8")
    router = SequentialRouter(
        [
            {
                "summary": "tentativa 1",
                "operations": [
                    {
                        "op": "replace",
                        "path": "svc.py",
                        "search": "valor = 1",
                        "replace": "value = 2",
                    }
                ],
            },
            {
                "summary": "tentativa 2",
                "operations": [
                    {
                        "op": "replace",
                        "path": "svc.py",
                        "search": "value = 1",
                        "replace": "value = 2",
                    }
                ],
            },
        ]
    )
    executor = LLMExecutor(
        router,
        agent_name="backend_executor",
        workspace_runtime=_runtime(tmp_path),
        max_autocorrect_rounds=1,
    )

    outcome = await executor.execute(_task(), {})

    assert target.read_text(encoding="utf-8") == "value = 2\n"
    second_prompt = router.requests[1].messages[0].content
    assert "apply: failed" in second_prompt
    assert "não encontrado" in second_prompt
    workspace = outcome["result"]["workspace"]
    assert workspace["apply_errors"] == []
    assert workspace["autocorrect"]["iterations"][0]["failed_checks"] == ["apply"]
    assert workspace["autocorrect"]["stopped_reason"] == "checks_passed_or_skipped"
    # payload sem `files` legado quando o modelo respondeu com operations
    assert "files" not in outcome["result"]


# --------------------------------------------------------------------------
# Judge
# --------------------------------------------------------------------------


class ApprovingRouter:
    async def complete(self, tier, request):
        return CompletionResult(
            text="ok",
            parsed={
                "criteria": [
                    {"criterion": "arquivo atualizado", "score": 1.0, "reasoning": "ok"}
                ],
                "failures": [],
                "required_changes": [],
                "overall_score": 1.0,
                "approved": True,
            },
            model="fake",
            provider="fake",
            usage=Usage(),
            cost_usd=0.0,
            latency_ms=0.0,
        )


@pytest.mark.asyncio
async def test_judge_vetoes_when_operations_failed_to_apply():
    task = _task(
        result={
            "summary": "editado",
            "workspace": {
                "applied_files": [],
                "file_diffs": [],
                "apply_errors": [
                    {
                        "path": "svc.py",
                        "operation": "replace",
                        "error": "trecho `search` não encontrado no arquivo atual",
                    }
                ],
            },
        }
    )

    outcome = await LLMJudge(ApprovingRouter()).evaluate(task, {})

    assert outcome.evaluation.approved is False
    assert outcome.evaluation.score <= 0.4
    assert any(
        f.startswith("[apply] replace svc.py") for f in outcome.evaluation.failures
    )
    assert "apply" in outcome.evaluation.validated_by


def test_minimal_change_requires_create_operations_only():
    created = _task(
        result={
            "workspace": {
                "file_diffs": [
                    {
                        "path": "new.py",
                        "change_type": "created",
                        "changed": True,
                        "operation": "create",
                    }
                ]
            }
        }
    )
    replaced = _task(
        result={
            "workspace": {
                "file_diffs": [
                    {
                        "path": "new.py",
                        "change_type": "created",
                        "changed": True,
                        "operation": "create",
                    },
                    {
                        "path": "old.py",
                        "change_type": "modified",
                        "changed": True,
                        "operation": "replace",
                    },
                ]
            }
        }
    )
    assert no_existing_file_modified(created) is True
    assert no_existing_file_modified(replaced) is False
    assert no_existing_file_modified(_task(result={"summary": "sem workspace"})) is None


# --------------------------------------------------------------------------
# Publicação de PR
# --------------------------------------------------------------------------


def test_collect_publishable_changes_prefers_final_content_and_tracks_deletes():
    tasks = [
        {
            "result": {
                "summary": "t1",
                "operations": [
                    {"op": "replace", "path": "a.py", "search": "x", "replace": "y"}
                ],
                "workspace": {
                    "published_files": [{"path": "a.py", "content": "y\n"}],
                    "deleted_paths": ["old.py"],
                },
            }
        },
        {  # tarefa antiga, formato legado sem workspace
            "result": {"files": [{"path": "b.py", "content": "b\n"}]}
        },
    ]
    files, deletions = collect_publishable_changes(tasks)
    assert files == [
        {"path": "a.py", "content": "y\n"},
        {"path": "b.py", "content": "b\n"},
    ]
    assert deletions == ["old.py"]


# --------------------------------------------------------------------------
# Grounding: arquivo inteiro para permitir replace
# --------------------------------------------------------------------------


def test_grounding_collector_can_include_small_files_whole(tmp_path: Path):
    (tmp_path / "app").mkdir()
    lines = [f"line_{i} = {i}" for i in range(1, 121)]
    (tmp_path / "app" / "svc.py").write_text("\n".join(lines) + "\n", encoding="utf-8")

    windowed = RepositoryGroundingCollector(
        str(tmp_path), max_excerpt_lines=10
    ).collect("svc line_5")
    whole = RepositoryGroundingCollector(
        str(tmp_path), max_excerpt_lines=10, full_file_max_bytes=64_000
    ).collect("svc line_5")

    windowed_item = next(e for e in windowed["evidence"] if e["path"] == "app/svc.py")
    whole_item = next(e for e in whole["evidence"] if e["path"] == "app/svc.py")
    assert windowed_item["line_end"] - windowed_item["line_start"] + 1 <= 10
    assert (whole_item["line_start"], whole_item["line_end"]) == (1, 120)
    assert "line_120 = 120" in whole_item["excerpt"]
