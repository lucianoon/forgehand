import asyncio
import base64
import os
from unittest.mock import AsyncMock

import pytest

from app.factory.git_auth import GitAuthentication, GitHubRepositoryAccess
from app.factory.workspace import GitCommandError, SafeGitRunner
from app.models.factory import RepositoryTarget


SOURCE = "https://github.com/acme/private.git"
TOKEN = "fake-sensitive-credential-123456"


@pytest.mark.parametrize("encoded", [False, True])
async def test_git_error_redacts_before_truncation(monkeypatch, tmp_path, encoded):
    secret = (
        base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        if encoded
        else TOKEN
    )
    process = AsyncMock()
    process.returncode = 1
    process.communicate.return_value = (
        ("before " + secret).encode(),
        ("before " + secret).encode(),
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("GITHUB_TOKEN", "unrelated-parent-secret")
    monkeypatch.setenv("GIT_TRACE_CURL", "1")
    environment_before = dict(os.environ)
    authentication = GitAuthentication(SOURCE, TOKEN)
    with pytest.raises(GitCommandError) as caught:
        await SafeGitRunner(tmp_path, max_output_chars=18).run(
            ["ls-remote", SOURCE], authentication=authentication
        )
    assert caught.value.result.stdout == "before ***"
    assert caught.value.result.stderr == "before ***"
    assert TOKEN not in repr(authentication) + str(caught.value)
    assert secret not in repr(caught.value.result)
    args, kwargs = spawn.call_args
    assert TOKEN not in repr(args)
    assert "GITHUB_TOKEN" not in kwargs["env"]
    assert "GIT_TRACE_CURL" not in kwargs["env"]
    assert dict(os.environ) == environment_before


async def test_ancestor_git_config_cannot_rewrite_authenticated_source(tmp_path):
    ancestor = SafeGitRunner(tmp_path)
    await ancestor.run(["init", "--quiet"])
    await ancestor.run(
        ["config", "url.https://evil.invalid/.insteadOf", "https://github.com/"]
    )
    control = tmp_path / "factory"
    control.mkdir()
    result = await SafeGitRunner(control).run(
        ["ls-remote", "--get-url", SOURCE],
        authentication=GitAuthentication(SOURCE, TOKEN),
    )
    assert result.stdout.strip() == SOURCE


@pytest.mark.parametrize("bare", [False, True])
async def test_authentication_rejects_repository_as_control_directory(tmp_path, bare):
    runner = SafeGitRunner(tmp_path)
    await runner.run(["init", "--bare"] if bare else ["init", "--quiet"])
    with pytest.raises(ValueError, match="diretório de controle"):
        await runner.run(
            ["ls-remote", "--get-url", SOURCE],
            authentication=GitAuthentication(SOURCE, TOKEN),
        )


@pytest.mark.parametrize(
    "args",
    [
        ["status"],
        ["config", "--list"],
        ["clone", "--config=http.sslVerify=false", SOURCE],
    ],
)
async def test_authentication_cannot_be_passed_to_local_or_overridden_commands(
    tmp_path, args
):
    with pytest.raises(ValueError, match="somente para transporte"):
        await SafeGitRunner(tmp_path).run(
            args, authentication=GitAuthentication(SOURCE, TOKEN)
        )


async def test_provider_failure_does_not_disclose_response_body():
    provider = AsyncMock()
    provider.token.side_effect = RuntimeError(f"remote response: {TOKEN}")
    with pytest.raises(RuntimeError, match="Não foi possível obter") as caught:
        await GitHubRepositoryAccess(provider).for_repository(
            RepositoryTarget(full_name="acme/private"), SOURCE
        )
    assert TOKEN not in str(caught.value)
    assert caught.value.__suppress_context__


async def test_anonymous_https_disables_redirects_and_parent_environment(
    monkeypatch, tmp_path
):
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (b"", b"")
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.extraHeader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"Authorization: {TOKEN}")
    await SafeGitRunner(tmp_path).run(["ls-remote", SOURCE])
    env = spawn.call_args.kwargs["env"]
    assert env["GIT_CONFIG_KEY_0"] == "http.followRedirects"
    assert env["GIT_CONFIG_VALUE_0"] == "false"
    assert TOKEN not in repr(env)


@pytest.mark.parametrize(
    "source",
    [
        "http://github.com/acme/private.git",
        "https://user:secret@github.com/acme/private.git",
        SOURCE + "?token=x",
        SOURCE + "#fragment",
    ],
)
def test_authentication_rejects_unsafe_destination(source):
    with pytest.raises(ValueError):
        GitAuthentication(source, TOKEN)
