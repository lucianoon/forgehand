import json

import httpx
import pytest

from app.evaluation.benchmark import (
    BenchmarkCase,
    BenchmarkPolicy,
    CaseResult,
    run_case,
    summarize,
)


def test_benchmark_summary_tracks_completion_first_pass_and_cost():
    report = summarize(
        [
            CaseResult(
                case_id="a",
                completed=True,
                first_pass=True,
                cost_usd=0.1,
                elapsed_seconds=2,
            ),
            CaseResult(
                case_id="b",
                completed=False,
                first_pass=False,
                cost_usd=0.2,
                elapsed_seconds=4,
            ),
        ]
    )

    assert report["completion_rate"] == 0.5
    assert report["first_pass_rate"] == 0.5
    assert abs(report["total_cost_usd"] - 0.3) < 1e-9
    assert report["average_elapsed_seconds"] == 3
    assert report["p95_elapsed_seconds"] == 4
    assert report["quality_gate"]["passed"] is False


def test_benchmark_quality_gate_can_pass_custom_policy():
    report = summarize(
        [
            CaseResult(
                case_id="a",
                completed=True,
                first_pass=True,
                cost_usd=0.1,
                elapsed_seconds=2,
            )
        ],
        BenchmarkPolicy(
            min_completion_rate=1,
            min_first_pass_rate=1,
            max_average_cost_usd=0.2,
            max_p95_elapsed_seconds=3,
        ),
    )

    assert report["quality_gate"]["passed"] is True


@pytest.mark.asyncio
async def test_run_case_forwards_delivery_and_reports_it():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/workflows":
            seen.update(json.loads(request.content))
            return httpx.Response(202, json={"workflow_id": "wf-1"})
        return httpx.Response(
            200,
            json={
                "workflow_id": "wf-1",
                "status": "completed",
                "current_stage": "completed",
                "iteration": 1,
                "usage": {"tokens": 10, "cost_usd": 0.01},
                "tasks": [
                    {
                        "id": "t",
                        "title": "t",
                        "capability": "backend",
                        "status": "completed",
                        "attempts": 1,
                    }
                ],
                "pending_decision": None,
                "final_output": "ok",
                "error": None,
                "delivery": {"ci_state": "success", "url": "https://gh.test/pr/1"},
            },
        )

    client = httpx.AsyncClient(
        base_url="https://api.test", transport=httpx.MockTransport(handler)
    )
    case = BenchmarkCase(
        id="c",
        project_id="p",
        request="corrija os testes que falham",
        delivery={"repository": "acme/svc", "wait_for_checks": True},
    )
    result = await run_case(client, case, "key")

    assert seen["delivery"] == {"repository": "acme/svc", "wait_for_checks": True}
    assert result.completed is True
    assert result.delivery == {"ci_state": "success", "url": "https://gh.test/pr/1"}

    plain = BenchmarkCase(id="d", project_id="p", request="sem entrega configurada")
    seen.clear()
    await run_case(client, plain, "key")
    assert "delivery" not in seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["failed", "cancelled", "awaiting_decision", "completed", "ready_for_human_review"],
)
async def test_first_pass_requires_successful_completion(status):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"workflow_id": "wf"})
        return httpx.Response(
            200,
            json={
                "status": status,
                "tasks": [{"attempts": 1}],
                "usage": {"cost_usd": 0.2},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://api.test", transport=httpx.MockTransport(handler)
    ) as client:
        result = await run_case(
            client,
            BenchmarkCase(id="c", project_id="p", request="execute este pedido"),
            "key",
        )
    assert result.first_pass is (status in {"completed", "ready_for_human_review"})


def test_summary_counts_failure_cost_and_rejects_inconsistent_first_pass():
    report = summarize(
        [
            CaseResult(case_id="a", completed=True, first_pass=True, cost_usd=0.2),
            CaseResult(case_id="b", completed=False, first_pass=True, cost_usd=0.8),
        ]
    )
    assert report["first_pass_rate"] == 0.5
    assert report["cost_per_completed_usd"] == pytest.approx(1.0)


def test_no_completions_has_no_defined_cost_per_completion():
    report = summarize([CaseResult(case_id="a", cost_usd=0.8)])
    assert report["cost_per_completed_usd"] is None


def test_empty_suite_never_passes_even_with_zero_rate_thresholds():
    report = summarize(
        [], BenchmarkPolicy(min_completion_rate=0, min_first_pass_rate=0)
    )
    assert report["quality_gate"]["passed"] is False
