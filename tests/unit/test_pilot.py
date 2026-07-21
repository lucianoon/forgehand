from app.evaluation.benchmark import BenchmarkCase
from app.evaluation.pilot import expand_cases


def test_pilot_expands_scenarios_into_named_rounds():
    cases = [
        BenchmarkCase(
            id="security",
            project_id="pilot",
            request="Revise a segurança deste repositório.",
        )
    ]

    expanded = expand_cases(cases, 3)

    assert [case.id for case in expanded] == [
        "security-run-1",
        "security-run-2",
        "security-run-3",
    ]


def test_pilot_expands_single_selected_scenario():
    cases = [
        BenchmarkCase(
            id="architecture-review",
            project_id="pilot",
            request="Analise a arquitetura.",
        )
    ]

    expanded = expand_cases(cases, 2)

    assert {case.id for case in expanded} == {
        "architecture-review-run-1",
        "architecture-review-run-2",
    }
