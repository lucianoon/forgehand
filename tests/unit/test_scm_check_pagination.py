"""CI evidence must include every current check, including later API pages."""

import httpx
import pytest

from app.infrastructure.scm import GitHubSCMClient, SCMError


def check(run_id, conclusion="success"):
    return {
        "id": run_id,
        "name": f"test-{run_id}",
        "status": "completed",
        "conclusion": conclusion,
    }


def status(index, state="success"):
    return {"id": index, "context": f"ci-{index}", "state": state}


async def fetch(handler):
    async with httpx.AsyncClient(
        base_url="https://api.github.test", transport=httpx.MockTransport(handler)
    ) as client:
        return await GitHubSCMClient("test-token", client=client).fetch_checks(
            "acme/repo", "a" * 40
        )


@pytest.mark.asyncio
async def test_failed_check_run_on_second_page_prevents_success():
    pages = []

    def handler(request):
        if request.url.path.endswith("/check-runs"):
            page = int(request.url.params.get("page", 1))
            pages.append(page)
            runs = (
                [check(i) for i in range(1, 101)]
                if page == 1
                else [check(101, "failure")]
            )
            return httpx.Response(200, json={"check_runs": runs, "total_count": 101})
        if request.url.path.endswith("/annotations"):
            return httpx.Response(
                200,
                json=[
                    {"path": "app.py", "start_line": 7, "message": "late-page failure"}
                ],
            )
        return httpx.Response(200, json={"statuses": [], "total_count": 0})

    result = await fetch(handler)
    assert result.state == "failure"
    assert len(result.checks) == 101
    assert pages == [1, 2]
    assert result.failure_paths == ["app.py"]
    assert any("late-page failure" in line for line in result.failures)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [31, 101])
async def test_failed_status_beyond_default_page_prevents_success(count):
    requests = []
    statuses = [status(i) for i in range(1, count)] + [status(count, "failure")]

    def handler(request):
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": [], "total_count": 0})
        assert request.url.path.endswith("/status")
        requests.append(request)
        per_page = int(request.url.params.get("per_page", 30))
        offset = (int(request.url.params.get("page", 1)) - 1) * per_page
        return httpx.Response(
            200,
            json={
                "statuses": statuses[offset : offset + per_page],
                "total_count": count,
            },
        )

    result = await fetch(handler)
    assert result.state == "failure"
    assert len(result.checks) == count
    assert len(requests) == (1 if count == 31 else 2)


@pytest.mark.asyncio
async def test_latest_checks_and_status_contexts_keep_success_semantics():
    def handler(request):
        if request.url.path.endswith("/check-runs"):
            # filter=all would reintroduce obsolete failures from earlier runs.
            assert request.url.params.get("filter") == "latest"
            return httpx.Response(
                200,
                json={
                    "check_runs": [check(1, "skipped"), check(2, "neutral")],
                    "total_count": 2,
                },
            )
        # Combined status is the latest state per context, not historical statuses.
        assert request.url.path.endswith("/status")
        return httpx.Response(
            200,
            json={
                "statuses": [
                    status(1),
                    {"context": "forgehand/delivery", "state": "failure"},
                ],
                "total_count": 2,
            },
        )

    result = await fetch(handler)
    assert result.state == "success"
    assert [item.name for item in result.checks] == ["test-1", "test-2", "ci-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"check_runs": None},
        {"check_runs": [None]},
        {"check_runs": [], "total_count": 1},
        {"check_runs": [check(1)], "total_count": 0},
        {"check_runs": [], "total_count": True},
        {"check_runs": [], "total_count": -1},
        {"check_runs": [], "total_count": "0"},
        {"check_runs": [], "total_count": 3001},
    ],
)
async def test_malformed_or_incomplete_check_inventory_is_rejected(payload):
    def handler(request):
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"statuses": [status(1)], "total_count": 1})

    with pytest.raises(SCMError, match="check_inventory"):
        await fetch(handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["check_runs", "statuses"])
async def test_inventory_change_between_pages_is_rejected(kind):
    def handler(request):
        is_checks = request.url.path.endswith("/check-runs")
        key = "check_runs" if is_checks else "statuses"
        if key != kind:
            return httpx.Response(200, json={key: [], "total_count": 0})
        page = int(request.url.params.get("page", 1))
        make = check if is_checks else status
        return httpx.Response(
            200,
            json={
                key: [make(i) for i in range(1, 101)] if page == 1 else [make(101)],
                "total_count": 101 if page == 1 else 102,
            },
        )

    with pytest.raises(SCMError, match="check_inventory"):
        await fetch(handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["check_runs", "statuses"])
async def test_duplicate_identity_cannot_hide_missing_inventory(kind):
    def handler(request):
        is_checks = request.url.path.endswith("/check-runs")
        key = "check_runs" if is_checks else "statuses"
        if key != kind:
            return httpx.Response(200, json={key: [], "total_count": 0})
        page = int(request.url.params.get("page", 1))
        make = check if is_checks else status
        return httpx.Response(
            200,
            json={
                key: [make(i) for i in range(1, 101)] if page == 1 else [make(1)],
                "total_count": 101,
            },
        )

    with pytest.raises(SCMError, match="check_inventory"):
        await fetch(handler)


@pytest.mark.asyncio
async def test_inventory_without_count_stops_at_bounded_page_limit():
    pages = []

    def handler(request):
        if request.url.path.endswith("/check-runs"):
            page = int(request.url.params.get("page", 1))
            pages.append(page)
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        check(i) for i in range(page * 100, (page + 1) * 100)
                    ]
                },
            )
        return httpx.Response(200, json={"statuses": []})

    with pytest.raises(SCMError, match="check_inventory_limit"):
        await fetch(handler)
    assert pages == list(range(1, 31))


@pytest.mark.asyncio
async def test_next_link_prevents_premature_success_on_short_page():
    def handler(request):
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={"check_runs": [check(1)], "total_count": 1},
                headers={"Link": '<https://api.github.test/next>; rel="next"'},
            )
        return httpx.Response(200, json={"statuses": []})

    with pytest.raises(SCMError, match="check_inventory"):
        await fetch(handler)
