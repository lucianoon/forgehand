"""Checkout authorization over real local Git data, without HTTPS or real secrets.

The adapter records each remote operation and its credential, then translates
its URL to a local fixture. Transport isolation is covered by separate tests.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.factory.git_auth import GitHubRepositoryAccess
from app.factory.workspace import (
    GitCommandError,
    GitCommandResult,
    LocalGitWorkspaceManager,
    SafeGitRunner,
)
from app.models.factory import (
    DirectWorkOrderSource,
    RepositoryTarget,
    WorkOrder,
    WorkspaceLifecycle,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(os.name != "posix", reason="factory mode requires POSIX"),
]


class RotatingTokens:
    def __init__(self):
        self.issued = []
        self.unavailable = False

    async def token(self):
        if self.unavailable:
            raise RuntimeError("provider-body-with-fixture-secret")
        token = f"fixture-token-{len(self.issued) + 1}"
        self.issued.append(token)
        return token


@dataclass
class Invocation:
    args: tuple[str, ...]
    source: str | None
    authentication: object


class LocalRemoteAdapter(SafeGitRunner):
    def __init__(self, root, remote):
        super().__init__(root)
        self.remote = remote
        self.calls = []
        self.require_credentials = True
        self.revoked = False

    @property
    def remote_calls(self):
        return [call for call in self.calls if call.source]

    async def run(self, args, *, cwd=None, check=True, authentication=None):
        sources = [arg for arg in args if arg.startswith("https://")]
        # A URL occurring inside a config key/value is local configuration,
        # not a remote operation that should receive a credential.
        command = args[2:] if args[0] == "--git-dir" else args
        remote_operation = command[0] in {"clone", "fetch", "ls-remote"}
        source = sources[0] if sources and remote_operation else None
        self.calls.append(Invocation(tuple(args), source, authentication))
        if source:
            if self.revoked or (self.require_credentials and authentication is None):
                raise GitCommandError(
                    GitCommandResult(tuple(args), 128, "", "Authentication failed")
                )
            if authentication is not None:
                assert authentication.source == source
            mapped = [str(self.remote) if arg == source else arg for arg in args]
            result = await super().run(mapped, cwd=cwd, check=check)
            if command[0] == "clone":
                # A real HTTPS clone records the canonical remote URL. Restore
                # that non-secret fixture metadata after local URL translation.
                await super().run(
                    [
                        "config",
                        "--file",
                        str(Path(args[-1]) / "config"),
                        "remote.origin.url",
                        source,
                    ]
                )
            return result
        assert authentication is None, "Local Git command received a credential"
        return await super().run(args, cwd=cwd, check=check)


def order(host="github.com", name="acme/widgets"):
    return WorkOrder(
        source=DirectWorkOrderSource(),
        repository=RepositoryTarget(full_name=name, scm_host=host),
        requested_outcome="Update the example while preserving existing changes.",
        acceptance_criteria=["Regression tests pass"],
    )


@pytest.fixture
async def lab(tmp_path):
    remote = tmp_path / "remote"
    git = SafeGitRunner(tmp_path)
    await git.run(["init", "--initial-branch=main", str(remote)])
    await git.run(["config", "user.name", "Fixture"], cwd=remote)
    await git.run(["config", "user.email", "fixture@example.test"], cwd=remote)
    (remote / "README.md").write_text("base\n")
    await git.run(["add", "README.md"], cwd=remote)
    await git.run(["commit", "-m", "fixture"], cwd=remote)
    root = tmp_path / "factory"
    root.mkdir()
    provider = RotatingTokens()
    runner = LocalRemoteAdapter(root, remote)
    manager = LocalGitWorkspaceManager(
        root,
        approved_hosts=["github.com", "git.example.test"],
        runner=runner,
        repository_access=GitHubRepositoryAccess(provider),
    )
    return manager, runner, provider, root, git


async def test_new_cached_and_active_checkouts_request_fresh_remote_credentials(lab):
    manager, runner, provider, _, _ = lab
    first = await manager.provision("first", order())
    second = await manager.provision("second", order())
    (Path(first.local_path) / "README.md").write_text("work in progress\n")
    active = manager.transition(first, WorkspaceLifecycle.ACTIVE)
    resumed = await manager.reconstruct(active)

    assert [
        call.args[0] if call.args[0] != "--git-dir" else call.args[2]
        for call in runner.remote_calls
    ] == ["clone", "fetch", "fetch", "ls-remote"]
    assert [
        call.authentication.token for call in runner.remote_calls
    ] == provider.issued
    assert len(provider.issued) == 4
    assert resumed.state == WorkspaceLifecycle.ACTIVE
    assert resumed.id == first.id and resumed.base_sha == second.base_sha
    assert (Path(first.local_path) / "README.md").read_text() == "work in progress\n"
    assert (Path(second.local_path) / "README.md").read_text() == "base\n"
    assert all(call.authentication is None for call in runner.calls if not call.source)


async def test_partial_cache_and_provisioning_recovery_use_current_credentials(lab):
    manager, runner, provider, _, git = lab
    lease = await manager.provision("interrupted", order())
    cache = manager._cache_path(lease.repository)
    await git.run(["--git-dir", str(cache), "config", "remote.origin.promisor", "true"])
    await git.run(
        [
            "--git-dir",
            str(cache),
            "config",
            "remote.origin.partialclonefilter",
            "blob:none",
        ]
    )
    interrupted = manager.transition(lease, WorkspaceLifecycle.PROVISIONING)
    before = len(provider.issued)
    resumed = await manager.reconstruct(interrupted)

    calls = runner.remote_calls[before:]
    assert len(calls) == 2
    assert calls[0].args[0] == "ls-remote"
    assert "--refetch" in calls[1].args
    assert [call.authentication.token for call in calls] == provider.issued[before:]
    assert resumed.state == WorkspaceLifecycle.ACTIVE
    assert resumed.base_sha == lease.base_sha
    assert (Path(resumed.local_path) / "README.md").read_text() == "base\n"


@pytest.mark.parametrize("entry", ["new", "cache", "active", "provisioning"])
@pytest.mark.parametrize("failure", ["revoked", "missing"])
async def test_current_access_required_even_when_private_data_is_cached(
    lab, entry, failure
):
    manager, runner, provider, root, _ = lab
    existing = None
    if entry != "new":
        existing = await manager.provision("existing", order())
        state = (
            WorkspaceLifecycle.PROVISIONING
            if entry == "provisioning"
            else WorkspaceLifecycle.ACTIVE
        )
        existing = manager.transition(existing, state)
        (Path(existing.local_path) / "README.md").write_text("preserve this work\n")
    before = existing.model_dump(mode="json") if existing else None
    issued = len(provider.issued)
    if failure == "revoked":
        runner.revoked = True
    else:
        manager = LocalGitWorkspaceManager(
            root,
            approved_hosts=["github.com"],
            runner=runner,
        )

    with pytest.raises(GitCommandError, match="Authentication failed"):
        if entry in {"new", "cache"}:
            await manager.provision("denied", order())
        else:
            await manager.reconstruct(existing)

    assert len(provider.issued) == issued + (failure == "revoked")
    assert manager.journal.get("denied") is None
    if existing:
        assert manager.journal.get("existing").model_dump(mode="json") == before
        assert (
            Path(existing.local_path) / "README.md"
        ).read_text() == "preserve this work\n"


@pytest.mark.parametrize("entry", ["provision", "reconstruct"])
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("url.https://evil.invalid/.insteadOf", "https://github.com/"),
        ("http.https://github.com/acme/widgets.git.proxy", "http://proxy.invalid"),
        ("http.https://github.com/acme/widgets.git.sslVerify", "false"),
        ("core.hooksPath", "/tmp/untrusted-hooks"),
        ("include.path", "/tmp/untrusted-config"),
        ("remote.origin.url", "https://github.com/other/widgets.git"),
    ],
)
async def test_unsafe_cache_configuration_rejected_before_credentials(
    lab, entry, key, value
):
    manager, runner, provider, _, git = lab
    lease = await manager.provision("existing", order())
    before = manager.journal.get("existing").model_dump(mode="json")
    cache = manager._cache_path(lease.repository)
    await git.run(["config", "--file", str(cache / "config"), key, value])
    issued, remote_calls = len(provider.issued), len(runner.remote_calls)

    with pytest.raises(ValueError, match="cache Git"):
        if entry == "provision":
            await manager.provision("denied", order())
        else:
            await manager.reconstruct(lease)

    assert len(provider.issued) == issued
    assert len(runner.remote_calls) == remote_calls
    assert manager.journal.get("existing").model_dump(mode="json") == before
    assert manager.journal.get("denied") is None


@pytest.mark.parametrize(
    "source",
    [
        "http://github.com/acme/widgets.git",
        "ssh://github.com/acme/widgets.git",
        "https://github.com/other/widgets.git",
        "https://github.com/acme/other.git",
        "https://github.com/acme/widgets.git/child",
        "https://github.com/acme/widgets.git?auth=anything",
        "https://github.com/acme/widgets.git#fragment",
        "https://username:fixture-secret@github.com/acme/widgets.git",
        "https://github.com:443/acme/widgets.git",
        "https://github.com:invalid/acme/widgets.git",
        "https://git.example.test/acme/widgets.git",
        " https://github.com/acme/widgets.git",
    ],
)
async def test_noncanonical_remote_rejected_before_credentials(lab, source):
    _, runner, provider, root, _ = lab
    manager = LocalGitWorkspaceManager(
        root,
        approved_hosts=["github.com", "git.example.test"],
        runner=runner,
        repository_access=GitHubRepositoryAccess(provider),
        repository_url_resolver=lambda _: source,
    )
    with pytest.raises(ValueError):
        await manager.provision("denied", order())
    assert not provider.issued and not runner.remote_calls
    assert manager.journal.get("denied") is None


@pytest.mark.parametrize(
    "name", ["../widgets", "acme/..", "acme?/widgets", "acme/widgets#suffix"]
)
async def test_invalid_repository_components_rejected_before_credentials(lab, name):
    manager, runner, provider, _, _ = lab
    with pytest.raises(ValueError):
        await manager.provision("denied", order(name=name))
    assert not provider.issued and not runner.remote_calls


async def test_other_approved_hosts_never_receive_github_token(lab):
    manager, runner, provider, _, _ = lab
    runner.require_credentials = False
    lease = await manager.provision("foreign", order(host="git.example.test"))
    await manager.reconstruct(lease)
    assert not provider.issued
    assert len(runner.remote_calls) == 3
    assert all(call.authentication is None for call in runner.remote_calls)
    assert all(
        call.source == "https://git.example.test/acme/widgets.git"
        for call in runner.remote_calls
    )


async def test_local_repository_never_requests_or_receives_token(lab):
    _, runner, provider, root, _ = lab
    manager = LocalGitWorkspaceManager(
        root,
        approved_hosts=["github.com"],
        runner=runner,
        repository_access=GitHubRepositoryAccess(provider),
        repository_url_resolver=lambda _: str(runner.remote),
        allow_local_repositories=True,
    )
    lease = await manager.provision("local", order())
    await manager.reconstruct(lease)
    assert not provider.issued and not runner.remote_calls
    assert all(call.authentication is None for call in runner.calls)


async def test_provider_failure_is_sanitized_before_git_or_lease_changes(lab):
    manager, runner, provider, _, _ = lab
    lease = await manager.provision("existing", order())
    before = manager.journal.get("existing").model_dump(mode="json")
    calls = len(runner.remote_calls)
    provider.unavailable = True
    with pytest.raises(RuntimeError) as error:
        await manager.reconstruct(lease)
    assert "fixture-secret" not in str(error.value)
    assert "provider-body" not in str(error.value)
    assert len(runner.remote_calls) == calls
    assert manager.journal.get("existing").model_dump(mode="json") == before


@pytest.mark.parametrize("entry", ["provision", "reconstruct"])
@pytest.mark.parametrize("linked", [False, True], ids=["file", "symlink"])
async def test_commondir_cannot_replace_validated_cache_configuration(lab, entry, linked):
    import shutil

    manager, runner, provider, root, git = lab
    lease = await manager.provision("existing", order())
    cache = manager._cache_path(lease.repository)
    source = "https://github.com/acme/widgets.git"
    common = root / "other-common.git"
    shutil.copytree(cache, common)
    await git.run([
        "config", "--file", str(common / "config"),
        "url.https://evil.invalid/.insteadOf", "https://github.com/",
    ])
    if linked:
        pointer = root / "common-pointer"
        pointer.write_text(str(common) + "\n")
        (cache / "commondir").symlink_to(pointer)
    else:
        (cache / "commondir").write_text(str(common) + "\n")
    # --get-url performs no HTTP. It proves Git reads common/config even
    # though the apparent cache/config still contains only permitted settings.
    effective = await git.run(["--git-dir", str(cache), "ls-remote", "--get-url", source])
    assert effective.stdout.strip() == "https://evil.invalid/acme/widgets.git"
    issued, remote_calls = len(provider.issued), len(runner.remote_calls)
    before = manager.journal.get("existing").model_dump(mode="json")

    with pytest.raises(ValueError, match="cache Git|Cache Git"):
        if entry == "provision":
            await manager.provision("denied", order())
        else:
            await manager.reconstruct(lease)

    assert len(provider.issued) == issued
    assert len(runner.remote_calls) == remote_calls
    assert manager.journal.get("existing").model_dump(mode="json") == before
    assert manager.journal.get("denied") is None
