import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.factory.workspace import (
    GitCommandError,
    LocalGitWorkspaceManager,
    SafeGitRunner,
)
from app.models.factory import (
    DirectWorkOrderSource,
    RepositoryTarget,
    WorkOrder,
    WorkspaceLease,
    WorkspaceLifecycle,
    WorkspaceRetention,
)


@pytest.mark.asyncio
async def test_safe_git_runner_uses_argv_without_shell(tmp_path: Path) -> None:
    runner = SafeGitRunner(tmp_path)

    with pytest.raises(GitCommandError):
        await runner.run(["status;touch", "escaped"])

    assert not (tmp_path / "escaped").exists()


@pytest.mark.asyncio
async def test_safe_git_runner_confines_working_directory(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    runner = SafeGitRunner(root)

    with pytest.raises(ValueError, match="fora da raiz"):
        await runner.run(["status"], cwd=outside)


@pytest.mark.asyncio
async def test_safe_git_runner_bounds_output(tmp_path: Path) -> None:
    runner = SafeGitRunner(tmp_path, max_output_chars=12)
    await runner.run(["init", "--quiet"])
    result = await runner.run(
        ["status", "--short", "--branch"],
        check=False,
    )

    assert len(result.stdout) <= 12


def test_safe_git_runner_redacts_url_credentials() -> None:
    assert (
        SafeGitRunner.redact("fatal: https://user:secret@github.com/acme/repo")
        == "fatal: https://***@github.com/acme/repo"
    )


async def _seed_remote(tmp_path: Path) -> tuple[Path, SafeGitRunner]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    runner = SafeGitRunner(tmp_path)
    await runner.run(["init", "--bare", str(remote)])
    await runner.run(["init", str(seed)])
    await runner.run(["config", "user.email", "factory@example.test"], cwd=seed)
    await runner.run(["config", "user.name", "Factory Test"], cwd=seed)
    (seed / "README.md").write_text("base\n", encoding="utf-8")
    await runner.run(["add", "README.md"], cwd=seed)
    await runner.run(["commit", "-m", "base"], cwd=seed)
    await runner.run(["branch", "-M", "main"], cwd=seed)
    await runner.run(["remote", "add", "origin", str(remote)], cwd=seed)
    await runner.run(["push", "origin", "main"], cwd=seed)
    return remote, runner


def _order() -> WorkOrder:
    return WorkOrder(
        source=DirectWorkOrderSource(),
        repository=RepositoryTarget(full_name="acme/widgets"),
        requested_outcome="Alterar o arquivo de exemplo.",
        acceptance_criteria=["Teste passa"],
    )


@pytest.mark.asyncio
async def test_manager_materializes_blobs_from_legacy_partial_cache(tmp_path):
    remote, runner = await _seed_remote(tmp_path)
    await runner.run(
        ["--git-dir", str(remote), "config", "uploadpack.allowFilter", "true"]
    )
    manager = LocalGitWorkspaceManager(
        tmp_path / "factory",
        approved_hosts=["github.com"],
        repository_url_resolver=lambda _: str(remote),
        allow_local_repositories=True,
    )
    cache = manager._cache_path(_order().repository)
    await runner.run(
        ["clone", "--bare", "--filter=blob:none", remote.as_uri(), str(cache)]
    )
    # Real partial-clone transport, unlike a local path clone which ignores filters.
    missing = await runner.run(
        ["--git-dir", str(cache), "rev-list", "--objects", "--all", "--missing=print"]
    )
    assert "?" in missing.stdout
    lease = await manager.provision("partial-cache", _order())
    assert Path(lease.local_path, "README.md").read_text() == "base\n"
    result = await runner.run(["fsck", "--no-reflogs"], cwd=lease.local_path)
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_manager_pins_sha_and_creates_exclusive_branch(tmp_path: Path) -> None:
    remote, _ = await _seed_remote(tmp_path)
    manager = LocalGitWorkspaceManager(
        tmp_path / "factory",
        approved_hosts=["github.com"],
        repository_url_resolver=lambda _: str(remote),
        allow_local_repositories=True,
    )

    lease = await manager.provision("workflow-1", _order())

    assert Path(lease.local_path, "README.md").read_text() == "base\n"
    assert lease.branch == "forgehand/workflow-1"
    assert len(lease.base_sha) == 40
    resumed = await manager.reconstruct(lease)
    assert resumed.id == lease.id
    assert resumed.base_sha == lease.base_sha
    assert resumed.state == WorkspaceLifecycle.ACTIVE


@pytest.mark.asyncio
async def test_restart_rebuilds_journaled_partial_checkout_at_same_pin(tmp_path):
    remote, _ = await _seed_remote(tmp_path)
    root = tmp_path / "factory"
    manager = LocalGitWorkspaceManager(
        root,
        approved_hosts=["github.com"],
        repository_url_resolver=lambda _: str(remote),
        allow_local_repositories=True,
    )
    lease = await manager.provision("restart", _order())
    partial = manager.transition(lease, WorkspaceLifecycle.PROVISIONING)
    restarted = LocalGitWorkspaceManager(
        root,
        approved_hosts=["github.com"],
        repository_url_resolver=lambda _: str(remote),
        allow_local_repositories=True,
    )
    recovered = await restarted.provision("restart", _order())
    assert recovered.id == partial.id
    assert recovered.base_sha == partial.base_sha
    assert Path(recovered.local_path, "README.md").read_text() == "base\n"


@pytest.mark.asyncio
async def test_git_cancellation_terminates_child_before_return(tmp_path):
    import os
    import sys

    marker = tmp_path / "pid"
    runner = SafeGitRunner(tmp_path, git_executable=sys.executable)
    job = asyncio.create_task(
        runner.run(
            [
                "-c",
                "import os,time,pathlib;pathlib.Path('pid').write_text(str(os.getpid()));time.sleep(60)",
            ]
        )
    )
    async with asyncio.timeout(5):
        while not marker.exists():
            await asyncio.sleep(0.01)
    pid = int(marker.read_text())
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_manager_isolates_concurrent_workspaces(tmp_path: Path) -> None:
    remote, _ = await _seed_remote(tmp_path)
    manager = LocalGitWorkspaceManager(
        tmp_path / "factory",
        approved_hosts=["github.com"],
        repository_url_resolver=lambda _: str(remote),
        allow_local_repositories=True,
    )

    first, second = await asyncio.gather(
        manager.provision("workflow-a", _order()),
        manager.provision("workflow-b", _order()),
    )
    Path(first.local_path, "README.md").write_text("changed\n", encoding="utf-8")

    assert first.local_path != second.local_path
    assert Path(second.local_path, "README.md").read_text() == "base\n"
    assert len(list((tmp_path / "factory" / "cache").iterdir())) == 1


@pytest.mark.asyncio
async def test_manager_rejects_unapproved_host_before_git(tmp_path: Path) -> None:
    manager = LocalGitWorkspaceManager(
        tmp_path / "factory", approved_hosts=["github.com"]
    )
    order = _order().model_copy(
        update={
            "repository": RepositoryTarget(
                full_name="acme/widgets", scm_host="evil.test"
            )
        }
    )

    with pytest.raises(ValueError, match="não está aprovado"):
        await manager.provision("workflow-1", order)


@pytest.mark.asyncio
async def test_cleanup_is_idempotent_and_preserves_repository_cache(
    tmp_path: Path,
) -> None:
    remote, _ = await _seed_remote(tmp_path)
    manager = LocalGitWorkspaceManager(
        tmp_path / "factory",
        approved_hosts=["github.com"],
        repository_url_resolver=lambda _: str(remote),
        allow_local_repositories=True,
    )
    active = await manager.provision("cleanup-a", _order())
    cache_entries = list((tmp_path / "factory" / "cache").iterdir())

    released = await manager.cleanup(active)
    released_again = await manager.cleanup(released)

    assert released.state == WorkspaceLifecycle.RELEASED
    assert released_again.state == WorkspaceLifecycle.RELEASED
    assert not Path(active.local_path).exists()
    assert cache_entries and all(path.exists() for path in cache_entries)


@pytest.mark.asyncio
async def test_cleanup_honors_retention_and_rejects_foreign_path(
    tmp_path: Path,
) -> None:
    remote, _ = await _seed_remote(tmp_path)
    manager = LocalGitWorkspaceManager(
        tmp_path / "factory",
        approved_hosts=["github.com"],
        repository_url_resolver=lambda _: str(remote),
        allow_local_repositories=True,
    )
    active = await manager.provision("cleanup-retained", _order())
    retained = active.model_copy(
        update={
            "retention": WorkspaceRetention(
                retain_until=datetime.now(timezone.utc) + timedelta(hours=1),
                reason="diagnóstico",
            )
        }
    )

    result = await manager.cleanup(retained)

    assert result.state == WorkspaceLifecycle.RETAINED
    assert Path(active.local_path).exists()
    foreign = active.model_copy(update={"local_path": str(tmp_path / "outside")})
    with pytest.raises(ValueError, match="fora da raiz"):
        await manager.cleanup(foreign)


@pytest.mark.asyncio
async def test_cleanup_releases_workspace_after_retention_expires(
    tmp_path: Path,
) -> None:
    remote, runner = await _seed_remote(tmp_path)
    manager = LocalGitWorkspaceManager(
        tmp_path / "factory",
        approved_hosts=["github.com"],
        repository_url_resolver=lambda _: str(remote),
        allow_local_repositories=True,
    )
    active = await manager.provision("cleanup-expired", _order())
    retained = active.model_copy(
        update={
            "state": WorkspaceLifecycle.RETAINED,
            "retention": WorkspaceRetention(
                retain_until=datetime.now(timezone.utc) - timedelta(hours=1),
                reason="diagnóstico concluído",
            ),
        }
    )

    released = await manager.cleanup(retained)
    remote_head = await runner.run(
        ["--git-dir", str(remote), "rev-parse", "refs/heads/main"]
    )

    assert released.state == WorkspaceLifecycle.RELEASED
    assert not Path(active.local_path).exists()
    assert released.id == retained.id
    assert released.retention == retained.retention
    assert remote_head.stdout.strip() == active.base_sha


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", [WorkspaceLifecycle.PROVISIONING, WorkspaceLifecycle.FAILED]
)
async def test_cleanup_removes_partially_provisioned_checkout(
    tmp_path: Path, state: WorkspaceLifecycle
) -> None:
    root = tmp_path / "factory"
    manager = LocalGitWorkspaceManager(root, approved_hosts=["github.com"])
    workspace = root / "workspaces" / "cleanup-partial"
    incomplete = workspace / "incomplete"
    incomplete.mkdir(parents=True)
    (incomplete / "download.tmp").write_text("partial clone", encoding="utf-8")
    lease = WorkspaceLease(
        workflow_id="cleanup-partial",
        repository=_order().repository,
        local_path=str(workspace),
        branch="forgehand/cleanup-partial",
        base_sha="a" * 40,
        state=state,
        failure_reason="clone interrompido",
    )
    assert not (workspace / ".git").exists()

    released = await manager.cleanup(lease)
    released_again = await manager.cleanup(released)

    assert released.state == WorkspaceLifecycle.RELEASED
    assert released_again.state == WorkspaceLifecycle.RELEASED
    assert not workspace.exists()
    assert released.id == lease.id
    assert released.failure_reason == lease.failure_reason
    assert (root / "cache").is_dir()


@pytest.mark.asyncio
async def test_cleanup_rejects_symlink_without_removing_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "factory"
    manager = LocalGitWorkspaceManager(root, approved_hosts=["github.com"])
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    workspace = root / "workspaces" / "cleanup-symlink"
    workspace.symlink_to(outside, target_is_directory=True)
    lease = WorkspaceLease(
        workflow_id="cleanup-symlink",
        repository=_order().repository,
        local_path=str(workspace),
        branch="forgehand/cleanup-symlink",
        base_sha="a" * 40,
    )

    with pytest.raises(ValueError, match="link simbólico"):
        await manager.cleanup(lease)

    assert workspace.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "preserve"


@pytest.mark.asyncio
async def test_cleanup_rejects_workspace_owned_by_another_workflow(
    tmp_path: Path,
) -> None:
    root = tmp_path / "factory"
    manager = LocalGitWorkspaceManager(root, approved_hosts=["github.com"])
    own_workspace = root / "workspaces" / "workflow-a"
    other_workspace = root / "workspaces" / "workflow-b"
    own_workspace.mkdir()
    other_workspace.mkdir()
    sentinel = other_workspace / "keep.txt"
    sentinel.write_text("other workflow", encoding="utf-8")
    foreign_lease = WorkspaceLease(
        workflow_id="workflow-a",
        repository=_order().repository,
        local_path=str(other_workspace),
        branch="forgehand/workflow-a",
        base_sha="a" * 40,
    )

    with pytest.raises(ValueError, match="ownership da lease inválido"):
        await manager.cleanup(foreign_lease)

    assert own_workspace.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "other workflow"
