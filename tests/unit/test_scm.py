from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.infrastructure.scm import GitHubSCMClient


@pytest.mark.asyncio
async def test_github_scm_publishes_branch_files_and_pull_request():
    requests: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path.endswith("/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "base-sha"}})
        if request.method == "GET" and "/git/ref/" in request.url.path:
            return httpx.Response(404, json={"message": "Not Found"})
        if request.method == "GET" and "/contents/" in request.url.path:
            return httpx.Response(404, json={"message": "Not Found"})
        if request.method == "GET" and request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/pulls"):
            return httpx.Response(
                201, json={"number": 42, "html_url": "https://github.test/pr/42"}
            )
        return httpx.Response(201, json={"ok": True})

    client = GitHubSCMClient(
        "token",
        client=httpx.AsyncClient(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ),
    )
    result = await client.publish_pull_request(
        repository="acme/service",
        base_branch="main",
        head_branch="forgehand/workflow-1",
        title="Forgehand delivery",
        body="Auditável",
        files=[{"path": "app/new.py", "content": "print('ok')\n"}],
    )

    assert result.number == 42
    assert result.branch == "forgehand/workflow-1"
    put = next(item for item in requests if item[0] == "PUT")
    assert base64.b64decode(str(put[2]["content"])).decode() == "print('ok')\n"
    assert requests[-1][1].endswith("/pulls")


@pytest.mark.asyncio
async def test_github_scm_retry_reuses_branch_file_and_open_pull_request():
    requests: list[tuple[str, str]] = []
    content = "already published\n"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if "/git/ref/" in request.url.path:
            return httpx.Response(200, json={"object": {"sha": "sha"}})
        if "/contents/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "sha": "file-sha",
                    "content": base64.b64encode(content.encode()).decode(),
                },
            )
        if request.method == "GET" and request.url.path.endswith("/pulls"):
            return httpx.Response(
                200,
                json=[{"number": 7, "html_url": "https://github.test/pr/7"}],
            )
        raise AssertionError(f"request inesperado: {request.method} {request.url}")

    client = GitHubSCMClient(
        "token",
        client=httpx.AsyncClient(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ),
    )
    result = await client.publish_pull_request(
        repository="acme/service",
        base_branch="main",
        head_branch="forgehand/workflow-1",
        title="Forgehand delivery",
        body="Auditável",
        files=[{"path": "app/new.py", "content": content}],
    )

    assert result.number == 7
    assert result.url == "https://github.test/pr/7"
    assert not any(method in {"POST", "PUT"} for method, _ in requests)
