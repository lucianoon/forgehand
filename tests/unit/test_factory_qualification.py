import json
from pathlib import Path

import httpx
import pytest

from app.evaluation.factory_fixtures import prepare_fixture
from app.evaluation.factory_qualification import (
    FactoryCase,
    FactoryResult,
    run_qualification,
    run_factory_case,
    summarize_factory,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["ready_for_human_review", "awaiting_decision", "running"]
)
async def test_case_submits_pin_records_evidence_and_cancels_nonterminal(
    tmp_path, monkeypatch, status
):
    from app.evaluation import factory_qualification as qualification

    requests = []
    case = FactoryCase(
        id="case-one",
        ecosystem="python",
        base_sha="a" * 40,
        request="Fix something useful",
        acceptance_criteria=["works"],
        expected_paths=["app.py"],
        hidden_case="defect",
        max_cost_usd=1,
        timeout_seconds=10,
    )

    async def verify(case, result, *args):
        result.hidden_check = result.scope_passed = True

    monkeypatch.setattr(qualification, "independent_check", verify)

    def handler(request):
        requests.append(request)
        if request.method == "POST" and request.url.path == "/workflows":
            body = json.loads(request.content)
            assert body["work_order"]["expected_base_sha"] == "a" * 40
            return httpx.Response(202, json={"workflow_id": "wf"})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "status": status,
                    "workspace": {"base_sha": "a" * 40},
                    "usage": {"tokens": 5, "cost_usd": 1},
                    "tasks": [{"attempts": 1}],
                    "delivery": {
                        "attempts": 1,
                        "pull_request_number": 7,
                        "commit_sha": "b" * 40,
                        "ci_state": "success",
                    },
                },
            )
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        result = await run_factory_case(
            client, client, case, "acme/r", "key", tmp_path, "/socket", "run"
        )
    assert result.pull_request == 7
    assert result.branch == "forgehand/wf"
    assert result.green == (status == "ready_for_human_review")
    assert any(request.url.path.endswith("/cancel") for request in requests) == (
        status != "ready_for_human_review"
    )


def green(case_id):
    return FactoryResult(
        case_id=case_id,
        outcome="ready_for_human_review",
        repository="acme/r",
        branch="forgehand/wf",
        pull_request=1,
        commit_sha="a" * 40,
        ci="success",
        hidden_check=True,
        scope_passed=True,
        first_pass=True,
        tokens=10,
        cost_usd=0.1,
        elapsed_seconds=10,
    )


def test_release_gate_requires_four_of_five_independently_green_prs():
    results = [green(str(i)) for i in range(5)]
    results[4].hidden_check = False
    report = summarize_factory(results, sandbox_qualified=True)
    assert report["release_gate"]["passed"]
    assert report["green_pr_rate"] == 0.8
    assert report["tokens"] == 50
    assert report["p95_seconds"] == 10
    results[3].scope_passed = False
    assert not summarize_factory(results, sandbox_qualified=True)["release_gate"][
        "passed"
    ]


@pytest.mark.parametrize(
    "field,value", [("isolation_violations", 1), ("technical_failure", "unclassified")]
)
def test_isolation_or_unclassified_failure_blocks_release(field, value):
    results = [green(str(i)) for i in range(5)]
    setattr(results[4], field, value)
    assert not summarize_factory(results, sandbox_qualified=True)["release_gate"][
        "passed"
    ]
    assert not summarize_factory(results[:4], sandbox_qualified=True)["release_gate"][
        "passed"
    ]


@pytest.mark.asyncio
async def test_budget_stops_cases_before_api_calls():
    case = FactoryCase(
        id="case-one",
        ecosystem="python",
        base_sha="a" * 40,
        request="Fix something useful",
        acceptance_criteria=["works"],
        expected_paths=["app.py"],
        hidden_case="defect",
        max_cost_usd=1,
        timeout_seconds=10,
    )

    def reject(request):
        raise AssertionError("budget-exhausted cases must not call the API")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        report = await run_qualification(
            client,
            client,
            [case],
            {"python": "acme/r"},
            "test-key",
            Path("."),
            "/unused",
            0.5,
        )
    assert report["results"][0]["outcome"] == "budget_exhausted"
    assert not report["release_gate"]["passed"]


@pytest.mark.parametrize("ecosystem", ["python", "node"])
def test_fixture_reset_creates_new_clean_repository_with_pinned_sha(
    tmp_path, ecosystem
):
    root = Path(__file__).resolve().parents[2] / "benchmarks" / "factory"
    first, sha = prepare_fixture(ecosystem, tmp_path, root)
    (first / "unexpected").write_text("dirty")
    second, other_sha = prepare_fixture(ecosystem, tmp_path, root)
    cases = json.loads((root / "cases.json").read_text())
    assert first != second
    assert sha == other_sha
    assert all(
        case["base_sha"] == sha for case in cases if case["ecosystem"] == ecosystem
    )
    assert not (second / "unexpected").exists()
    assert (first / "unexpected").exists()  # Never resets user directories.
