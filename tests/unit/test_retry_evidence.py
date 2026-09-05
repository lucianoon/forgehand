"""A checkpointed graph retry must publish the whole task's current changes."""

from datetime import datetime, timezone

import pytest

from app.agents.criteria import evaluate_objective_criteria
from app.agents.executor import LLMExecutor
from app.infrastructure.scm import collect_publishable_changes
from app.infrastructure.workspace_runtime import (
    CommandObjectiveValidator,
    LocalWorkspaceRuntime,
)
from app.models.task import AgentTask, Capability, TaskAttempt
from tests.unit.test_workspace_runtime import StaticRouter


def task():
    return AgentTask(
        title="Apply discount once",
        description="Fix orders.total and add a regression",
        capability=Capability.BACKEND,
        acceptance_criteria=[
            {"text": "single discount", "kind": "content_contains",
             "path": "orders.py", "pattern": "return amount"},
        ],
    )


async def execute(root, current, operations, **kwargs):
    executor = LLMExecutor(
        StaticRouter({"summary": "repair", "operations": operations}),
        "executor",
        workspace_runtime=LocalWorkspaceRuntime(
            str(root), apply_files_enabled=True, **kwargs
        ),
    )
    return await executor.execute(current, {})


def checkpoint_retry(current, outcome):
    return AgentTask.model_validate({
        **current.model_dump(mode="json"),
        "result": outcome["result"],
        "attempts": [TaskAttempt(
            attempt_number=1, agent_name="executor", model="fake",
            started_at=datetime.now(timezone.utc),
        ).model_dump(mode="json")],
    })


@pytest.mark.asyncio
async def test_graph_retry_retains_first_edit_for_criteria_and_publication(tmp_path):
    (tmp_path / "orders.py").write_text("return amount * discount\n")
    current = task()
    first = await execute(tmp_path, current, [{
        "op": "replace", "path": "orders.py",
        "search": "return amount * discount", "replace": "return amount",
    }])
    retry = checkpoint_retry(current, first)
    second = await execute(tmp_path, retry, [{
        "op": "create", "path": "test_orders.py", "content": "# regression\n",
    }])
    retry.result = second["result"]
    files, deleted = collect_publishable_changes([retry.model_dump(mode="json")])
    assert {item["path"]: item["content"] for item in files} == {
        "orders.py": "return amount\n", "test_orders.py": "# regression\n",
    }
    assert deleted == []
    assert evaluate_objective_criteria(retry, {}, {})[0].passed is True


@pytest.mark.asyncio
async def test_retry_reads_current_bytes_and_revalidates_even_without_operations(tmp_path):
    class Runner:
        calls = 0

        async def run(self, command, root, output_limit):
            self.calls += 1
            return {"exit_code": self.calls - 1, "stdout": "", "stderr": ""}

    runner = Runner()
    validator = CommandObjectiveValidator(
        name="pytest", command="pytest", workspace_root=str(tmp_path),
        command_runner=runner,
    )
    current = task()
    (tmp_path / "orders.py").write_text("return original\n")
    first = await execute(tmp_path, current, [{
        "op": "replace", "path": "orders.py",
        "search": "return original", "replace": "return amount",
    }], command_feedback_runners=[validator])
    assert first["result"]["workspace"]["command_feedback"][0]["passed"] is True
    (tmp_path / "orders.py").write_text("return changed_on_disk\n")
    retry = checkpoint_retry(current, first)
    # A checkpoint's absolute root is only historical metadata.
    retry.result["workspace"]["workspace_root"] = "/not/the/authorized/root"
    second = await execute(tmp_path, retry, [], command_feedback_runners=[validator])
    workspace = second["result"]["workspace"]
    assert runner.calls == 2
    assert workspace["command_feedback"][0]["passed"] is False
    assert workspace["published_files"] == [
        {"path": "orders.py", "content": "return changed_on_disk\n"},
    ]
    assert "-return original" in workspace["file_diffs"][0]["diff"]
    assert "+return changed_on_disk" in workspace["file_diffs"][0]["diff"]
    retry.result = second["result"]
    assert evaluate_objective_criteria(retry, {}, {})[0].passed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_exists", [False, True])
@pytest.mark.parametrize("deleted_by_operation", [False, True])
async def test_retry_deletions_never_resurrect_prior_content(
    tmp_path, initially_exists, deleted_by_operation
):
    current = task()
    target = tmp_path / "orders.py"
    if initially_exists:
        target.write_text("original\n")
    first = await execute(tmp_path, current, [{
        "op": "create", "path": "orders.py", "content": "return amount\n",
    }])
    if not deleted_by_operation:
        target.unlink()
    second = await execute(
        tmp_path, checkpoint_retry(current, first),
        [{"op": "delete", "path": "orders.py"}] if deleted_by_operation else [],
    )
    files, deleted = collect_publishable_changes([{"result": second["result"]}])
    assert files == []
    assert deleted == (["orders.py"] if initially_exists else [])
    if not initially_exists:
        assert second["result"]["workspace"]["file_diffs"] == []


@pytest.mark.asyncio
async def test_retry_does_not_inherit_evidence_from_a_different_task(tmp_path):
    first = await execute(tmp_path, task(), [{
        "op": "create", "path": "orders.py", "content": "return amount\n",
    }])
    second = await execute(tmp_path, checkpoint_retry(task(), first), [])
    assert collect_publishable_changes([{"result": second["result"]}]) == ([], [])


@pytest.mark.asyncio
async def test_retry_rejects_artifact_replaced_with_symlink_outside_runtime(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    current = task()
    first = await execute(root, current, [{
        "op": "create", "path": "orders.py", "content": "return amount\n",
    }])
    outside = tmp_path / "outside.py"
    outside.write_text("unrelated source")
    (root / "orders.py").unlink()
    (root / "orders.py").symlink_to(outside)
    with pytest.raises(ValueError, match="fora do workspace"):
        await execute(root, checkpoint_retry(current, first), [])


@pytest.mark.asyncio
async def test_retry_reverting_original_file_no_longer_claims_modification(tmp_path):
    current = task()
    (tmp_path / "orders.py").write_text("original\n")
    first = await execute(tmp_path, current, [{
        "op": "replace", "path": "orders.py", "search": "original",
        "replace": "return amount",
    }])
    second = await execute(tmp_path, checkpoint_retry(current, first), [{
        "op": "replace", "path": "orders.py", "search": "return amount",
        "replace": "original",
    }])
    diff = second["result"]["workspace"]["file_diffs"][0]
    assert diff["changed"] is False
    assert diff["change_type"] == "unchanged"
    assert diff["diff"] == ""


@pytest.mark.asyncio
async def test_delete_recreate_in_same_retry_keeps_original_creation_baseline(tmp_path):
    current = task()
    first = await execute(tmp_path, current, [{
        "op": "create", "path": "orders.py", "content": "v1",
    }])
    retry = checkpoint_retry(current, first)
    second = await execute(tmp_path, retry, [
        {"op": "delete", "path": "orders.py"},
        {"op": "create", "path": "orders.py", "content": "v2"},
    ])
    workspace = second["result"]["workspace"]
    assert workspace["file_diffs"][0]["change_type"] == "created"
    assert workspace["deleted_paths"] == []
    third = await execute(tmp_path, checkpoint_retry(retry, second), [
        {"op": "delete", "path": "orders.py"},
    ])
    assert collect_publishable_changes([{"result": third["result"]}]) == ([], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("external_edit", [False, True])
async def test_retry_from_old_checkpoint_preserves_file_created_criterion(
    tmp_path, external_edit
):
    current = task()
    first = await execute(tmp_path, current, [{
        "op": "create", "path": "orders.py", "content": "v1",
    }])
    # main's old serialized artifact format did not store original bytes.
    first["result"]["workspace"]["file_diffs"][0].pop("before_content")
    if external_edit:
        (tmp_path / "orders.py").write_text("v2")
    second = await execute(tmp_path, checkpoint_retry(current, first), [] if external_edit else [{
        "op": "replace", "path": "orders.py", "search": "v1", "replace": "v2",
    }])
    assert second["result"]["workspace"]["file_diffs"][0]["change_type"] == "created"
