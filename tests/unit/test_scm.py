"""SCM: commit atômico via Git Data API, PR idempotente, espera pelo CI e
credenciais (token estático / GitHub App)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.infrastructure.scm import (
    CheckRunsResult,
    GitHubAppTokenProvider,
    GitHubSCMClient,
    SCMError,
    StaticTokenProvider,
    build_token_provider_from_env,
    collect_publishable_changes,
    task_publishes_changes,
)


class GitHubMock:
    """Simula o subconjunto da API usado pelo cliente. Estado mínimo: branches,
    árvores conhecidas e PRs abertos."""

    def __init__(
        self,
        *,
        head_exists: bool = False,
        existing_paths: set[str] | None = None,
        open_pull: dict | None = None,
        check_runs: list[list[dict]] | None = None,
        statuses: list[dict] | None = None,
        annotations: dict[int, list[dict]] | None = None,
        tree_sha: str = "tree-1",
    ) -> None:
        self.requests: list[tuple[str, str, dict]] = []
        self.head_exists = head_exists
        self.existing_paths = existing_paths or set()
        self.open_pull = open_pull
        self.check_runs = list(check_runs or [])
        self.statuses = statuses or []
        self.annotations = annotations or {}
        self.tree_sha = tree_sha

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(self)
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        method = request.method
        self.requests.append((method, path, body))
        assert request.headers["Authorization"].startswith("Bearer ")

        if method == "GET" and path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "base-sha"}})
        if method == "GET" and "/git/ref/heads/" in path:
            if self.head_exists:
                return httpx.Response(200, json={"object": {"sha": "head-sha"}})
            return httpx.Response(404, json={"message": "Not Found"})
        if method == "POST" and path.endswith("/git/refs"):
            self.head_exists = True
            return httpx.Response(201, json={"ref": body["ref"]})
        if method == "GET" and "/git/commits/" in path:
            return httpx.Response(
                200, json={"sha": path.rsplit("/", 1)[-1], "tree": {"sha": "tree-0"}}
            )
        if method == "GET" and "/contents/" in path:
            rel = path.split("/contents/", 1)[1]
            if rel in self.existing_paths:
                return httpx.Response(200, json={"sha": "blob", "content": ""})
            return httpx.Response(404, json={"message": "Not Found"})
        if method == "POST" and path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": self.tree_sha})
        if method == "POST" and path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "commit-1"})
        if method == "PATCH" and "/git/refs/heads/" in path:
            return httpx.Response(200, json={"object": {"sha": body["sha"]}})
        if method == "GET" and path.endswith("/pulls"):
            return httpx.Response(200, json=[self.open_pull] if self.open_pull else [])
        if method == "POST" and path.endswith("/pulls"):
            self.open_pull = {"number": 42, "html_url": "https://gh.test/pr/42"}
            return httpx.Response(201, json=self.open_pull)
        if method == "GET" and path.endswith("/check-runs"):
            runs = self.check_runs.pop(0) if self.check_runs else []
            return httpx.Response(200, json={"check_runs": runs})
        if method == "GET" and path.endswith("/status"):
            return httpx.Response(
                200, json={"statuses": self.statuses, "state": "pending"}
            )
        if method == "GET" and "/check-runs/" in path and path.endswith("/annotations"):
            run_id = int(path.split("/check-runs/")[1].split("/")[0])
            return httpx.Response(200, json=self.annotations.get(run_id, []))
        if method == "POST" and "/statuses/" in path:
            return httpx.Response(201, json={"ok": True})
        return httpx.Response(
            500, json={"message": f"rota não simulada: {method} {path}"}
        )


def _client(mock: GitHubMock) -> GitHubSCMClient:
    return GitHubSCMClient("token", client=mock.client())


# --------------------------------------------------------------------------
# Publicação
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_creates_single_commit_with_all_files_and_deletions():
    mock = GitHubMock(existing_paths={"old.py"})
    result = await _client(mock).publish_pull_request(
        repository="acme/service",
        base_branch="main",
        head_branch="forgehand/wf",
        title="Forgehand delivery",
        body="Auditável",
        files=[
            {"path": "app/new.py", "content": "print('ok')\n"},
            {"path": "app/other.py", "content": "x = 1\n"},
        ],
        deletions=["old.py", "never-existed.py"],
        commit_message="forgehand: entrega",
    )

    assert result.number == 42
    assert result.commit_sha == "commit-1"
    assert result.changed is True
    methods = [(m, p.rsplit("/", 2)[-2:]) for m, p, _ in mock.requests]
    # branch criada a partir da base
    assert ("POST", ["git", "refs"]) in methods
    trees = [b for m, p, b in mock.requests if m == "POST" and p.endswith("/git/trees")]
    assert len(trees) == 1, "uma única árvore = um único commit"
    tree = trees[0]
    assert tree["base_tree"] == "tree-0"
    by_path = {entry["path"]: entry for entry in tree["tree"]}
    assert by_path["app/new.py"]["content"] == "print('ok')\n"
    assert by_path["app/other.py"]["mode"] == "100644"
    assert by_path["old.py"]["sha"] is None  # remoção
    assert "never-existed.py" not in by_path  # ausente na branch: ignorado
    commits = [
        b for m, p, b in mock.requests if m == "POST" and p.endswith("/git/commits")
    ]
    assert commits == [
        {"message": "forgehand: entrega", "tree": "tree-1", "parents": ["base-sha"]}
    ]
    patch = next(b for m, p, b in mock.requests if m == "PATCH")
    assert patch == {"sha": "commit-1", "force": False}
    assert not any(m in {"PUT", "DELETE"} for m, _, _ in mock.requests)


@pytest.mark.asyncio
async def test_publish_retry_with_identical_tree_makes_no_commit_and_reuses_pr():
    mock = GitHubMock(
        head_exists=True,
        open_pull={"number": 7, "html_url": "https://gh.test/pr/7"},
        tree_sha="tree-0",  # árvore resultante idêntica à do parent
    )
    result = await _client(mock).publish_pull_request(
        repository="acme/service",
        base_branch="main",
        head_branch="forgehand/wf",
        title="t",
        body="b",
        files=[{"path": "a.py", "content": "same\n"}],
    )

    assert result.number == 7
    assert result.changed is False
    assert result.commit_sha == "head-sha"  # parent = ponta da branch existente
    assert not any(
        p.endswith("/git/commits") and m == "POST" for m, p, _ in mock.requests
    )
    assert not any(m == "PATCH" for m, _, _ in mock.requests)
    assert not any(m == "POST" and p.endswith("/pulls") for m, p, _ in mock.requests)


@pytest.mark.asyncio
async def test_publish_rejects_bad_inputs_and_surfaces_api_errors():
    client = _client(GitHubMock())
    with pytest.raises(ValueError, match="owner/name"):
        await client.publish_pull_request(
            repository="bad",
            base_branch="main",
            head_branch="h",
            title="t",
            body="b",
            files=[{"path": "a", "content": "x"}],
        )
    with pytest.raises(ValueError, match="sem arquivos"):
        await client.publish_pull_request(
            repository="a/b",
            base_branch="main",
            head_branch="h",
            title="t",
            body="b",
            files=[],
            deletions=[],
        )

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Resource not accessible"})

    broken = GitHubSCMClient(
        "token",
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(failing)
        ),
    )
    with pytest.raises(SCMError, match="403"):
        await broken.publish_pull_request(
            repository="a/b",
            base_branch="main",
            head_branch="h",
            title="t",
            body="b",
            files=[{"path": "a", "content": "x"}],
        )


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def _run(name, status, conclusion=None, run_id=1, title=None, summary=None):
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "details_url": f"https://ci.test/{name}",
        "output": {"title": title, "summary": summary},
    }


@pytest.mark.asyncio
async def test_wait_for_checks_polls_until_success():
    mock = GitHubMock(
        check_runs=[
            [_run("test", "queued")],
            [_run("test", "in_progress")],
            [
                _run("test", "completed", "success"),
                _run("lint", "completed", "skipped", 2),
            ],
        ]
    )
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    result = await _client(mock).wait_for_checks(
        "acme/service", "commit-1", poll_interval_seconds=5, sleep=fake_sleep
    )

    assert result.state == "success"
    assert [c.name for c in result.checks] == ["test", "lint"]
    assert slept == [5, 5]


@pytest.mark.asyncio
async def test_wait_for_checks_reports_failures_with_summary_and_annotations():
    mock = GitHubMock(
        check_runs=[
            [
                _run(
                    "test",
                    "completed",
                    "failure",
                    11,
                    "2 failed",
                    "pytest: 2 failed, 40 passed",
                ),
                _run("lint", "completed", "success", 12),
            ]
        ],
        statuses=[
            {"context": "forgehand/delivery", "state": "pending"},  # ignorado (nosso)
            {"context": "codecov", "state": "failure", "description": "coverage -3%"},
        ],
        annotations={
            11: [
                {
                    "path": "tests/test_api.py",
                    "start_line": 88,
                    "message": "assert 1 == 2",
                },
                {
                    "path": "app/svc.py",
                    "start_line": 3,
                    "message": "E501 line too long",
                },
            ]
        },
    )

    result = await _client(mock).wait_for_checks("acme/service", "commit-1")

    assert result.state == "failure"
    assert result.failures[0].startswith("test: failure — 2 failed — pytest: 2 failed")
    assert "codecov: failure — coverage -3%" in result.failures
    assert "test: tests/test_api.py:88 assert 1 == 2" in result.failures
    assert not any("forgehand/delivery" in f for f in result.failures)
    assert {c.name for c in result.checks} == {"test", "lint", "codecov"}


@pytest.mark.asyncio
async def test_wait_for_checks_returns_none_without_ci_after_grace_and_pending_on_timeout():
    now = {"t": 0.0}

    def clock() -> float:
        return now["t"]

    async def fake_sleep(seconds: float) -> None:
        now["t"] += seconds

    no_ci = GitHubMock(check_runs=[[], [], [], []])
    result = await _client(no_ci).wait_for_checks(
        "acme/service",
        "sha",
        grace_seconds=30,
        poll_interval_seconds=10,
        sleep=fake_sleep,
        clock=clock,
    )
    assert result.state == "none"

    now["t"] = 0.0
    stuck = GitHubMock(check_runs=[[_run("test", "in_progress")] for _ in range(20)])
    result = await _client(stuck).wait_for_checks(
        "acme/service",
        "sha",
        timeout_seconds=25,
        poll_interval_seconds=10,
        sleep=fake_sleep,
        clock=clock,
    )
    assert result.state == "pending"
    assert result.checks[0].name == "test"


def test_check_runs_result_serializes_checks():
    from app.infrastructure.scm import CheckRun

    result = CheckRunsResult(
        state="failure",
        checks=[CheckRun(name="test", status="completed", conclusion="failure")],
    )
    assert result.as_dicts()[0]["conclusion"] == "failure"


# --------------------------------------------------------------------------
# Coleta de artefatos
# --------------------------------------------------------------------------


def test_collect_publishable_changes_prefers_final_content_and_tracks_deletes():
    tasks = [
        {
            "result": {
                "summary": "t1",
                "workspace": {
                    "published_files": [{"path": "a.py", "content": "y\n"}],
                    "deleted_paths": ["old.py"],
                },
            }
        },
        {"result": {"files": [{"path": "b.py", "content": "b\n"}]}},
    ]
    files, deletions = collect_publishable_changes(tasks)
    assert files == [
        {"path": "a.py", "content": "y\n"},
        {"path": "b.py", "content": "b\n"},
    ]
    assert deletions == ["old.py"]
    assert task_publishes_changes(tasks[0]["result"]) is True
    assert task_publishes_changes({"summary": "só análise"}) is False


# --------------------------------------------------------------------------
# Credenciais
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_token_provider_and_env_resolution(tmp_path):
    with pytest.raises(ValueError):
        StaticTokenProvider("")
    assert await StaticTokenProvider("abc").token() == "abc"

    assert build_token_provider_from_env({}) is None
    static = build_token_provider_from_env({"GITHUB_TOKEN": "ghp_x"})
    assert isinstance(static, StaticTokenProvider)

    key_file = tmp_path / "app.pem"
    key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfake\n", encoding="utf-8")
    app_provider = build_token_provider_from_env(
        {
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_INSTALLATION_ID": "456",
            "GITHUB_APP_PRIVATE_KEY_PATH": str(key_file),
            "GITHUB_TOKEN": "ignored",
        }
    )
    assert isinstance(app_provider, GitHubAppTokenProvider), "App tem prioridade"
    with pytest.raises(SCMError, match="Não foi possível ler"):
        build_token_provider_from_env(
            {
                "GITHUB_APP_ID": "1",
                "GITHUB_APP_INSTALLATION_ID": "2",
                "GITHUB_APP_PRIVATE_KEY_PATH": str(tmp_path / "missing.pem"),
            }
        )


@pytest.mark.asyncio
async def test_github_app_provider_signs_jwt_and_caches_installation_token():
    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )

    calls: list[dict] = []
    now = {"t": 1_700_000_000.0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/installations/456/access_tokens"
        bearer = request.headers["Authorization"].removeprefix("Bearer ")
        # relógio do teste é fixo em 2023: valida assinatura e claims, não expiração
        claims = jwt.decode(
            bearer,
            public_pem,
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        calls.append(claims)
        return httpx.Response(
            201,
            json={"token": f"ghs_{len(calls)}", "expires_at": "2023-11-14T23:13:20Z"},
        )

    provider = GitHubAppTokenProvider(
        "123",
        "456",
        pem,
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(handler)
        ),
        clock=lambda: now["t"],
    )

    first = await provider.token()
    second = await provider.token()
    assert first == second == "ghs_1", "token cacheado até perto de expirar"
    assert calls[0]["iss"] == "123"
    assert calls[0]["exp"] - calls[0]["iat"] == 600

    now["t"] = 1_700_003_560.0  # a 40s da expiração (< margem de 60s) → renova
    third = await provider.token()
    assert third == "ghs_2"
