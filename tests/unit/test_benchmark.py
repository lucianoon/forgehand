from app.evaluation.benchmark import BenchmarkPolicy, CaseResult, summarize


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
