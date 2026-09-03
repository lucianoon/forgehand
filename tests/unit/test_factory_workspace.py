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
    assert await manager.reconstruct(lease) == lease


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
