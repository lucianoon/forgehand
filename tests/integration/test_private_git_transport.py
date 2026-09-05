"""Real Git smart HTTPS against a local TLS server; no external credentials.

The test-only Git executable trusts the fixture certificate. Production TLS
verification remains enabled, and all requests stay on the loopback interface.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import ssl
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from app.factory.git_auth import GitAuthentication
from app.factory.workspace import GitCommandError, SafeGitRunner


_TOKEN = "forgehand-local-fake-token-497d6d"


def _basic(token: str) -> str:
    value = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"Basic {value}"


def _git(git: str, cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [git, "-c", f"core.hooksPath={os.devnull}", *args],
        cwd=cwd,
        env={
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return result.stdout.strip()


@dataclass
class _HTTPState:
    requests: list[tuple[str, str | None]] = field(default_factory=list)
    redirect_to: str | None = None
    reject_credentials: bool = False


def _start_https(
    root: Path, cert: Path, key: Path, git: str, state: _HTTPState
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Request logs must not accidentally expose authentication headers.
            return

        def do_GET(self) -> None:
            self._serve()

        def do_POST(self) -> None:
            self._serve()

        def _serve(self) -> None:
            state.requests.append((self.path, self.headers.get("Authorization")))
            if state.redirect_to:
                self.send_response(302)
                self.send_header("Location", state.redirect_to + self.path)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if state.reject_credentials or self.headers.get("Authorization") != _basic(
                _TOKEN
            ):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="private-fixture"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            parsed = urlsplit(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            completed = subprocess.run(
                [git, "http-backend"],
                input=body,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_PROJECT_ROOT": str(root),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "REQUEST_METHOD": self.command,
                    "PATH_INFO": parsed.path,
                    "QUERY_STRING": parsed.query,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(length),
                    "REMOTE_USER": "fixture",
                    "REMOTE_ADDR": self.client_address[0],
                    "SERVER_PROTOCOL": self.request_version,
                },
                capture_output=True,
                check=True,
                timeout=15,
            )
            headers, separator, payload = completed.stdout.partition(b"\r\n\r\n")
            if not separator:
                headers, separator, payload = completed.stdout.partition(b"\n\n")
            assert separator, "git http-backend did not emit CGI headers"
            fields = [line.decode().split(":", 1) for line in headers.splitlines()]
            status = next(
                (
                    int(value.strip().split()[0])
                    for name, value in fields
                    if name.lower() == "status"
                ),
                200,
            )
            self.send_response(status)
            for name, value in fields:
                if name.lower() != "status":
                    self.send_header(name, value.strip())
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@dataclass
class _PrivateGitFixture:
    git: str
    source: Path
    bare: Path
    client: Path
    wrapper: Path
    argv_log: Path
    url: str
    origin: _HTTPState
    redirect_target: _HTTPState
    destination_url: str

    def runner(self) -> SafeGitRunner:
        return SafeGitRunner(
            self.client, git_executable=str(self.wrapper), timeout_seconds=15
        )

    def calls(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.argv_log.read_text().splitlines()]


@pytest.fixture
def private_git(tmp_path: Path):
    git = shutil.which("git")
    openssl = shutil.which("openssl")
    if git is None or openssl is None:
        pytest.skip("local Git HTTPS integration requires git and openssl")
    cert, key = tmp_path / "localhost.crt", tmp_path / "localhost.key"
    config = tmp_path / "openssl.cnf"
    config.write_text(
        "[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=server\n"
        "[dn]\nCN=localhost\n"
        "[server]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
        "basicConstraints=critical,CA:TRUE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n"
        "extendedKeyUsage=serverAuth\n"
    )
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-config",
            str(config),
        ],
        capture_output=True,
        check=True,
        timeout=15,
    )
    source, repositories, client = (
        tmp_path / "source",
        tmp_path / "repositories",
        tmp_path / "client",
    )
    for path in (source, repositories, client):
        path.mkdir()
    bare = repositories / "private.git"
    _git(git, source, "init", "--initial-branch=main")
    (source / "README.md").write_text("First committed fixture content.\n")
    _git(git, source, "add", "README.md")
    _git(git, source, "commit", "-m", "Initial fixture")
    _git(git, repositories, "clone", "--bare", str(source), str(bare))
    argv_log = tmp_path / "git-argv.jsonl"
    recorder = tmp_path / "record-argv.py"
    recorder.write_text(
        "import json, os, sys\n"
        f"with open({str(argv_log)!r}, 'a') as log:\n"
        "    log.write(json.dumps({'argv': sys.argv[1:], 'authentication_present': "
        "any(value.lower().startswith('authorization:') for key, value in os.environ.items() "
        "if key.startswith('GIT_CONFIG_VALUE_'))}) + '\\n')\n"
    )
    wrapper = tmp_path / "git-with-fixture-ca"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"export GIT_SSL_CAINFO={shlex.quote(str(cert))}\n"
        f'{shlex.quote(sys.executable)} {shlex.quote(str(recorder))} "$@"\n'
        f'exec {shlex.quote(git)} "$@"\n'
    )
    wrapper.chmod(0o700)
    origin, target = _HTTPState(), _HTTPState()
    servers = [
        _start_https(repositories, cert, key, git, state) for state in (origin, target)
    ]
    try:
        yield _PrivateGitFixture(
            git,
            source,
            bare,
            client,
            wrapper,
            argv_log,
            f"https://localhost:{servers[0][0].server_port}/private.git",
            origin,
            target,
            f"https://localhost:{servers[1][0].server_port}",
        )
    finally:
        for server, thread in servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _assert_no_persisted_credentials(checkout: Path, evidence: str) -> None:
    for secret in (_TOKEN, _basic(_TOKEN), _basic(_TOKEN).removeprefix("Basic ")):
        assert secret not in evidence
        for path in checkout.rglob("*"):
            if path.is_file():
                assert secret.encode() not in path.read_bytes(), path.relative_to(
                    checkout
                )


@pytest.mark.asyncio
async def test_private_https_clone_and_fetch_keep_credentials_ephemeral(private_git):
    fixture = private_git
    runner = fixture.runner()
    authentication = GitAuthentication(source=fixture.url, token=_TOKEN)
    clone = await runner.run(
        ["clone", "--", fixture.url, "checkout"], authentication=authentication
    )
    checkout = fixture.client / "checkout"
    assert (checkout / "README.md").read_text() == "First committed fixture content.\n"
    (fixture.source / "README.md").write_text("Updated private fixture content.\n")
    _git(fixture.git, fixture.source, "add", "README.md")
    _git(fixture.git, fixture.source, "commit", "-m", "Updated fixture")
    expected_sha = _git(fixture.git, fixture.source, "rev-parse", "HEAD")
    _git(fixture.git, fixture.source, "push", str(fixture.bare), "main")
    fetch = await runner.run(
        ["--git-dir", str(checkout / ".git"), "fetch", "--", fixture.url, "main"],
        authentication=authentication,
    )
    head = await runner.run(["rev-parse", "FETCH_HEAD"], cwd=checkout)
    assert head.stdout.strip() == expected_sha
    configuration = await runner.run(["config", "--local", "--list"], cwd=checkout)
    assert f"remote.origin.url={fixture.url}" in configuration.stdout
    assert "extraheader" not in configuration.stdout.lower()
    calls = fixture.calls()
    assert [call["authentication_present"] for call in calls] == [
        True,
        True,
        False,
        False,
    ]
    evidence = (
        repr(authentication)
        + repr((clone, fetch, head, configuration))
        + json.dumps(calls)
    )
    _assert_no_persisted_credentials(checkout, evidence)
    assert fixture.origin.requests
    assert all(header == _basic(_TOKEN) for _, header in fixture.origin.requests)
    assert not fixture.redirect_target.requests


@pytest.mark.asyncio
async def test_private_https_redirect_never_sends_token_to_destination(private_git):
    fixture = private_git
    fixture.origin.redirect_to = fixture.destination_url
    result = await fixture.runner().run(
        ["clone", "--", fixture.url, "redirected"],
        authentication=GitAuthentication(source=fixture.url, token=_TOKEN),
        check=False,
    )
    assert result.exit_code != 0
    assert fixture.origin.requests and fixture.origin.requests[0][1] == _basic(_TOKEN)
    assert not fixture.redirect_target.requests
    assert _TOKEN not in repr(result) and _basic(_TOKEN) not in repr(result)
    assert not (fixture.client / "redirected" / "README.md").exists()


@pytest.mark.asyncio
async def test_private_https_unauthorized_fails_without_persisting_token(private_git):
    fixture = private_git
    fixture.origin.reject_credentials = True
    with pytest.raises(GitCommandError) as error:
        await fixture.runner().run(
            ["clone", "--", fixture.url, "unauthorized"],
            authentication=GitAuthentication(source=fixture.url, token=_TOKEN),
        )
    assert error.value.result.exit_code != 0
    assert fixture.origin.requests
    assert all(header == _basic(_TOKEN) for _, header in fixture.origin.requests)
    _assert_no_persisted_credentials(
        fixture.client, str(error.value) + repr(error.value.result)
    )
    assert not (fixture.client / "unauthorized" / "README.md").exists()


@pytest.mark.asyncio
async def test_private_https_rejects_untrusted_server_certificate(private_git):
    fixture = private_git
    # Use the actual executable, without the test wrapper's trusted CA. A valid
    # token must not turn a self-signed, untrusted TLS endpoint into a trusted one.
    runner = SafeGitRunner(
        fixture.client, git_executable=fixture.git, timeout_seconds=15
    )
    with pytest.raises(GitCommandError) as error:
        await runner.run(
            ["clone", "--", fixture.url, "untrusted"],
            authentication=GitAuthentication(source=fixture.url, token=_TOKEN),
        )
    assert error.value.result.exit_code != 0
    assert not fixture.origin.requests
    _assert_no_persisted_credentials(
        fixture.client, str(error.value) + repr(error.value.result)
    )
