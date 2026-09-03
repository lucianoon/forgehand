from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.service import WorkflowService
from app.factory.lifecycle import WorkspaceBusy, WorkspaceJournal
from app.factory.workspace import LocalGitWorkspaceManager
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import InMemoryWorkflowQueue
from app.models.factory import (
    RepositoryTarget,
    WorkspaceLease,
    WorkspaceLifecycle,
    WorkspaceRetention,
)


def manager_and_lease(tmp_path):
    manager = LocalGitWorkspaceManager(
        tmp_path / "factory", approved_hosts=["github.com"]
    )
    path = tmp_path / "factory" / "workspaces" / "wf"
    path.mkdir()
    (path / "output.py").write_text("output")
    lease = WorkspaceLease(
        workflow_id="wf",
        repository=RepositoryTarget(full_name="acme/r"),
        local_path=str(path),
        branch="forgehand/wf",
        base_sha="a" * 40,
        state=WorkspaceLifecycle.ACTIVE,
    )
    manager.journal.save(lease)
    return manager, lease


def service(manager, phase="ready_for_human_review", runner=None, settings=None):
    class Graph:
        values = {"phase": phase, "project_id": "p", "owner_client_id": "c"}

        async def aget_state(self, config):
            return SimpleNamespace(values=self.values)

        async def aupdate_state(self, config, values):
            self.values.update(values)

    return WorkflowService(
        Graph(),
        settings or Settings(),
        InMemoryWorkflowQueue(),
        False,
        workspace_manager=manager,
        build_runner=runner,
    )


@pytest.mark.asyncio
async def test_reconciler_survives_restart_and_cleanup_is_idempotent(tmp_path):
    manager, lease = manager_and_lease(tmp_path)
    restarted = LocalGitWorkspaceManager(
        tmp_path / "factory", approved_hosts=["github.com"]
    )
    worker = service(restarted)
    await worker.reconcile_workspaces()
    await worker.reconcile_workspaces()
    assert not Path(lease.local_path).exists()
    assert restarted.journal.get("wf").state == WorkspaceLifecycle.RELEASED
    assert restarted.journal.history("wf")[-1]["state"] == "released"


@pytest.mark.asyncio
async def test_reconciler_preserves_active_lock_and_failure_ttl(tmp_path):
    manager, lease = manager_and_lease(tmp_path)
    worker = service(manager, "failed")
    with manager.journal.exclusive("wf"):
        await worker.reconcile_workspaces()
        assert Path(lease.local_path).exists()
    await worker.reconcile_workspaces()
    retained = manager.journal.get("wf")
    assert retained.state == WorkspaceLifecycle.RETAINED
    assert Path(lease.local_path).exists()
    retained.retention = WorkspaceRetention(
        retain_until=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    manager.journal.save(retained)
    await worker.reconcile_workspaces()
    assert not Path(lease.local_path).exists()


@pytest.mark.asyncio
async def test_cleanup_requires_confirmed_container_termination(tmp_path):
    manager, lease = manager_and_lease(tmp_path)

    class Runner:
        safe = False

        async def retry_cleanup(self, workflow_id):
            assert Path(lease.local_path).exists()
            return self.safe

    runner = Runner()
    worker = service(manager, runner=runner)
    await worker.reconcile_workspaces()
    assert Path(lease.local_path).exists()
    assert manager.journal.get("wf").failure_reason == "sandbox_cleanup_pending"
    runner.safe = True
    await worker.reconcile_workspaces()
    assert not Path(lease.local_path).exists()


def test_inventory_and_exclusion_survive_new_instances(tmp_path):
    journal = WorkspaceJournal(tmp_path)
    journal.record_container("wf", "container-name", "ownership-token")
    other = WorkspaceJournal(tmp_path)
    assert other.containers() == {"wf": ("container-name", "ownership-token")}
    with journal.exclusive("wf"):
        with pytest.raises(WorkspaceBusy):
            with other.exclusive("wf"):
                pass
    other.forget_container("wf")
    assert journal.containers() == {}
