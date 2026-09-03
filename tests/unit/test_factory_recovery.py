import json

import httpx
import pytest

from app.infrastructure.scm import GitHubSCMClient, SCMError


@pytest.mark.asyncio
@pytest.mark.parametrize("crash", ["ref", "pull"])
async def test_restart_recovers_exact_commit_and_single_pull(crash):
    remote = {"head": None, "commit": None, "pull": None}
    writes = []
    interrupted = False

    def handler(request):
        nonlocal interrupted
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if request.method == "GET":
            if "/git/ref/" in path:
                return (
                    httpx.Response(200, json={"object": {"sha": remote["head"]}})
                    if remote["head"]
                    else httpx.Response(404)
                )
            if path.endswith("/git/commits/" + "b" * 40):
                return httpx.Response(200, json={"tree": {"sha": "base-tree"}})
            if "/git/commits/" in path:
                return httpx.Response(200, json=remote["commit"])
            if path.endswith("/pulls"):
                return httpx.Response(
                    200, json=[remote["pull"]] if remote["pull"] else []
                )
        writes.append(path)
        if path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "desired-tree"})
        if path.endswith("/git/commits"):
            remote["commit"] = {
                **body,
                "tree": {"sha": body["tree"]},
                "parents": [{"sha": p} for p in body["parents"]],
            }
            return httpx.Response(201, json={"sha": "c" * 40})
        if path.endswith("/git/refs"):
            remote["head"] = body["sha"]
            if crash == "ref" and not interrupted:
                interrupted = True
                raise httpx.ReadError("lost response")
            return httpx.Response(201, json={})
        if path.endswith("/pulls"):
            remote["pull"] = {
                "number": 7,
                "html_url": "https://github.com/acme/r/pull/7",
            }
            if crash == "pull" and not interrupted:
                interrupted = True
                raise httpx.ReadError("lost response")
            return httpx.Response(201, json=remote["pull"])
        raise AssertionError(path)

    args = dict(
        repository="acme/r",
        base_branch="main",
        head_branch="forgehand/wf",
        title="fix",
        body="body",
        files=[{"path": "x.py", "content": "fixed"}],
        pinned_base_sha="b" * 40,
    )

    async def publish():
        async with httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(handler)
        ) as http:
            return await GitHubSCMClient("test", client=http).publish_pull_request(
                **args
            )

    if crash == "ref":
        with pytest.raises(httpx.ReadError):
            await publish()
    else:
        assert (await publish()).number == 7
    recovered = await publish()
    assert recovered.commit_sha == "c" * 40
    assert recovered.number == 7
    assert recovered.changed is False
    assert sum(path.endswith("/git/commits") for path in writes) == 1
    assert sum(path.endswith("/pulls") for path in writes) == 1
    args["files"] = [{"path": "x.py", "content": "different"}]
    with pytest.raises(SCMError, match="factory_head_mismatch"):
        await publish()
