"""Reject statically shadowed tests even when a sandbox test command was green.

The PR regression is stored as JSON and parsed as syntax only. Generated test
code is never imported or executed on the host.
"""

import json
from pathlib import Path

import pytest

from app.agents.judge import LLMJudge
from app.api.container import build_objective_validation_pipeline, build_workspace_runtime
from app.infrastructure.settings import Settings
from tests.unit.test_judge_build_evidence import NoLLM, report, task


FIXTURE = Path(__file__).parents[1] / "fixtures/python_shadowed_tests.json"


@pytest.mark.asyncio
async def test_real_green_pr_with_shadowed_classes_is_vetoed_and_gets_repair_feedback(tmp_path):
    observed = json.loads(FIXTURE.read_text())
    path = tmp_path / observed["path"]
    path.parent.mkdir()
    path.write_text(observed["before"])
    settings = Settings(_env_file=None, executor_workspace_root=str(tmp_path),
                        executor_apply_files_enabled=True)
    pipeline = build_objective_validation_pipeline(settings, [])
    runtime = build_workspace_runtime(settings, pipeline)
    current = task(report())
    current.result = await runtime.apply(current, {"operations": [{
        "op": "replace", "path": observed["path"],
        "search": observed["before"], "replace": observed["after"],
    }]})
    outcome = await LLMJudge(NoLLM(), validation_pipeline=pipeline,
                             require_build_evidence=True).evaluate(current, {})
    assert outcome.evaluation.tests_passed is True
    assert not outcome.evaluation.approved, "Green test execution hid duplicate test classes"
    feedback = current.result["workspace"]["command_feedback"]
    integrity = next(signal for signal in feedback if signal["name"] == "python_test_integrity")
    assert integrity["passed"] is False
    for name in ("SmokeTests", "TestTotalWithDiscount"):
        assert name in integrity["details"]
    assert "python_test_integrity" in outcome.evaluation.validated_by


@pytest.mark.asyncio
@pytest.mark.parametrize("source,name", [
    ("def test_total(): pass\ndef test_total(): pass\n", "test_total"),
    ("async def test_total(): pass\ndef test_total(): pass\n", "test_total"),
    ("class TestTotal:\n def test_one(self): pass\nclass TestTotal: pass\n", "TestTotal"),
    ("class SmokeTests:\n def test_one(self): pass\nclass SmokeTests: pass\n", "SmokeTests"),
    ("class TestTotal:\n def test_one(self): pass\n async def test_one(self): pass\n", "TestTotal.test_one"),
    ("def test_total(): pass\nclass test_total: pass\n", "test_total"),
    ("if enabled:\n def test_total(): pass\n def test_total(): pass\n", "test_total"),
])
async def test_duplicate_definitions_in_one_scope_are_rejected(tmp_path, source, name):
    from app.infrastructure.python_test_integrity import PythonTestIntegrityValidator
    from app.models.task import Capability

    (tmp_path / "test_sample.py").write_text(source)
    signal = await PythonTestIntegrityValidator(str(tmp_path)).run(
        capability=Capability.TESTING, applied_files=["test_sample.py"],
    )
    assert signal.passed is False
    assert name in signal.details
    assert "linhas" in signal.details


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [
    "class TestA:\n def test_one(self): pass\nclass TestB:\n def test_one(self): pass\n",
    "class TestA:\n def test_one(self): pass\nclass TestB(TestA):\n def test_one(self): pass\n",
    "import pytest\n@pytest.mark.parametrize('value', [1, 2])\ndef test_total(value): pass\n",
    "def helper(): pass\ndef helper(): pass\ndef test_total(): pass\n",
    "if enabled:\n def test_total(): pass\nelse:\n def test_total(): pass\n",
    "from typing import overload\n@overload\ndef test_total(v: int): ...\n@overload\ndef test_total(v: str): ...\ndef test_total(v): pass\n",
    "import typing as t\n@t.overload\ndef test_total(v: int): ...\ndef test_total(v): pass\n",
    "def factory():\n def test_local(): pass\n def test_local(): pass\n return test_local\n",
    "raise RuntimeError('must never execute on host')\ndef test_total(): pass\n",
])
async def test_valid_scopes_and_parametrization_do_not_trigger_gate(tmp_path, source):
    from app.infrastructure.python_test_integrity import PythonTestIntegrityValidator
    from app.models.task import Capability

    (tmp_path / "test_sample.py").write_text(source)
    signal = await PythonTestIntegrityValidator(str(tmp_path)).run(
        capability=Capability.TESTING, applied_files=["test_sample.py"],
    )
    assert signal.passed is True


def changed_task(path, *, changed=True, change_type="modified"):
    current = task(report())
    current.result = {"workspace": {"file_diffs": [{
        "path": path, "changed": changed, "change_type": change_type,
    }]}}
    return current


@pytest.mark.asyncio
@pytest.mark.parametrize("path,changed,change_type", [
    ("app.py", True, "modified"),
    ("test_existing.py", False, "unchanged"),
    ("test_deleted.py", True, "deleted"),
])
async def test_only_modified_test_paths_are_checked(tmp_path, path, changed, change_type):
    from app.infrastructure.python_test_integrity import PythonTestIntegrityValidator

    duplicate = "def test_total(): pass\ndef test_total(): pass\n"
    (tmp_path / "test_existing.py").write_text(duplicate)
    (tmp_path / "app.py").write_text(duplicate)
    signal = await PythonTestIntegrityValidator(str(tmp_path)).validate(
        changed_task(path, changed=changed, change_type=change_type),
    )
    assert signal.passed is None


@pytest.mark.asyncio
async def test_judge_rereads_source_instead_of_trusting_cached_pass(tmp_path):
    from app.infrastructure.python_test_integrity import PythonTestIntegrityValidator

    current = changed_task("test_sample.py")
    current.result["workspace"]["command_feedback"] = [
        {"name": "python_test_integrity", "passed": True},
    ]
    current.result["workspace"]["workspace_root"] = "/untrusted/checkpoint/root"
    (tmp_path / "test_sample.py").write_text("def test_x(): pass\ndef test_x(): pass\n")
    assert (await PythonTestIntegrityValidator(str(tmp_path)).validate(current)).passed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["traversal", "absolute", "file_symlink", "directory_symlink", "hardlink", "fifo"])
async def test_unsafe_sources_are_rejected_without_reading_outside_workspace(tmp_path, unsafe):
    import os
    from app.infrastructure.python_test_integrity import PythonTestIntegrityValidator

    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "test_secret.py"
    external.write_text("SENTINEL = 'never include in diagnostics'\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    relative = "test_secret.py"
    if unsafe == "traversal":
        relative = "../outside/test_secret.py"
    elif unsafe == "absolute":
        relative = str(external)
    elif unsafe == "file_symlink":
        (workspace / relative).symlink_to(external)
    elif unsafe == "directory_symlink":
        (workspace / "linked").symlink_to(outside, target_is_directory=True)
        relative = "linked/test_secret.py"
    elif unsafe == "hardlink":
        os.link(external, workspace / relative)
    else:
        os.mkfifo(workspace / relative)
    signal = await PythonTestIntegrityValidator(str(workspace)).validate(changed_task(relative))
    assert signal.passed is False
    assert "never include in diagnostics" not in signal.details


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["def test_x(: SECRET_VALUE\n", "x" * (128 * 1024 + 1)])
async def test_invalid_or_oversized_source_fails_closed(tmp_path, source):
    from app.infrastructure.python_test_integrity import PythonTestIntegrityValidator

    (tmp_path / "test_sample.py").write_text(source)
    signal = await PythonTestIntegrityValidator(str(tmp_path)).validate(changed_task("test_sample.py"))
    assert signal.passed is False
    assert "SECRET_VALUE" not in signal.details


@pytest.mark.asyncio
async def test_unchanged_duplicate_test_file_does_not_trigger_autocorrection(tmp_path):
    duplicate = "def test_total(): pass\ndef test_total(): pass\n"
    (tmp_path / "test_existing.py").write_text(duplicate)
    settings = Settings(_env_file=None, executor_workspace_root=str(tmp_path), executor_apply_files_enabled=True)
    pipeline = build_objective_validation_pipeline(settings, [])
    runtime = build_workspace_runtime(settings, pipeline)
    result = await runtime.apply(task(report()), {"operations": [{
        "op": "create", "path": "test_existing.py", "content": duplicate,
    }]})
    assert result["workspace"]["file_diffs"][0]["changed"] is False
    assert result["workspace"]["command_feedback"] == []


@pytest.mark.asyncio
async def test_feedback_drives_existing_autocorrection_and_repair_clears_veto(tmp_path):
    from app.agents.executor import LLMExecutor
    from tests.unit.test_workspace_runtime import SequentialRouter

    duplicate = "class TestTotals:\n def test_one(self): pass\n def test_one(self): pass\n"
    corrected = "class TestTotals:\n def test_one(self): pass\n def test_two(self): pass\n"
    router = SequentialRouter([
        {"parsed": {"summary": "Add two tests", "operations": [{
            "op": "create", "path": "test_totals.py", "content": duplicate,
        }]}},
        {"parsed": {"summary": "Preserve both tests under unique names", "operations": [{
            "op": "replace", "path": "test_totals.py", "search": duplicate, "replace": corrected,
        }]}},
    ])
    settings = Settings(_env_file=None, executor_workspace_root=str(tmp_path), executor_apply_files_enabled=True)
    pipeline = build_objective_validation_pipeline(settings, [])
    executor = LLMExecutor(router, "test-executor", max_autocorrect_rounds=1,
                           workspace_runtime=build_workspace_runtime(settings, pipeline))
    current = task(report())
    output = await executor.execute(current, {})
    assert len(router.requests) == 2
    assert "python_test_integrity: failed" in router.requests[1].messages[0].content
    assert "TestTotals.test_one" in router.requests[1].messages[0].content
    assert (tmp_path / "test_totals.py").read_text() == corrected
    current.result = output["result"]
    assert (await LLMJudge(NoLLM(), validation_pipeline=pipeline,
                          require_build_evidence=True).evaluate(current, {})).evaluation.approved


@pytest.mark.asyncio
async def test_noop_retry_rechecks_prior_changed_test_source(tmp_path):
    settings = Settings(_env_file=None, executor_workspace_root=str(tmp_path), executor_apply_files_enabled=True)
    pipeline = build_objective_validation_pipeline(settings, [])
    runtime = build_workspace_runtime(settings, pipeline)
    current = task(report())
    valid = "def test_one(): pass\n"
    current.result = await runtime.apply(current, {"operations": [{
        "op": "create", "path": "test_example.py", "content": valid,
    }]})
    (tmp_path / "test_example.py").write_text(valid + valid)
    repeated = await runtime.apply(current, {"operations": []})
    signal = next(item for item in repeated["workspace"]["command_feedback"]
                  if item["name"] == "python_test_integrity")
    assert signal["passed"] is False
    assert repeated["workspace"]["published_files"][0]["content"] == valid + valid


@pytest.mark.asyncio
async def test_delete_only_edits_still_run_command_checks(tmp_path):
    from app.infrastructure.workspace_runtime import CommandObjectiveValidator

    class RecordingRunner:
        def __init__(self):
            self.calls = []

        async def run(self, command, workspace_root, output_limit):
            self.calls.append(command)
            return {"stdout": "", "stderr": "", "exit_code": 0}

    runner = RecordingRunner()
    command = CommandObjectiveValidator(name="pytest", command="pytest -q",
                                        workspace_root=str(tmp_path), file_suffixes={".py"},
                                        command_runner=runner)
    settings = Settings(_env_file=None, executor_workspace_root=str(tmp_path), executor_apply_files_enabled=True)
    pipeline = build_objective_validation_pipeline(settings, [command])
    runtime = build_workspace_runtime(settings, pipeline)
    (tmp_path / "test_deleted.py").write_text("def test_one(): pass\n")
    result = await runtime.apply(task(report()), {"operations": [{
        "op": "delete", "path": "test_deleted.py",
    }]})
    assert runner.calls == ["pytest -q"]
    assert [item["name"] for item in result["workspace"]["command_feedback"]] == ["pytest"]


@pytest.mark.asyncio
async def test_modified_source_disappearing_during_validation_fails_closed(tmp_path):
    from app.infrastructure.workspace_runtime import CommandObjectiveValidator

    class RemovingRunner:
        async def run(self, command, workspace_root, output_limit):
            (workspace_root / "test_new.py").unlink()
            return {"stdout": "", "stderr": "", "exit_code": 0}

    command = CommandObjectiveValidator(name="pytest", command="pytest -q",
                                        workspace_root=str(tmp_path), command_runner=RemovingRunner())
    settings = Settings(_env_file=None, executor_workspace_root=str(tmp_path), executor_apply_files_enabled=True)
    pipeline = build_objective_validation_pipeline(settings, [command])
    runtime = build_workspace_runtime(settings, pipeline)
    result = await runtime.apply(task(report()), {"operations": [{
        "op": "create", "path": "test_new.py", "content": "def test_one(): pass\n",
    }]})
    signal = next(item for item in result["workspace"]["command_feedback"]
                  if item["name"] == "python_test_integrity")
    assert signal["passed"] is False
    assert "FileNotFoundError" in signal["details"]
    # Reconciliation removed the vanished newly created file from net diffs,
    # but its failed current-attempt inspection must survive through the judge.
    current = task(report())
    current.result = result
    evaluated = await LLMJudge(NoLLM(), validation_pipeline=pipeline,
                               require_build_evidence=True).evaluate(current, {})
    assert evaluated.evaluation.tests_passed is True
    assert not evaluated.evaluation.approved
