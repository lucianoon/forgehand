"""Evals: orçamento fechado, relatório e gates, sem chamar LLM."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.evaluation.benchmark import BenchmarkPolicy, CaseResult
from app.evaluation.evals import load_policy, render_markdown, run_evals, write_reports

CASES = [
    {"id": "a", "project_id": "p", "request": "pedido com tamanho suficiente", "max_cost_usd": 0.5},
    {"id": "b", "project_id": "p", "request": "outro pedido com tamanho ok", "max_cost_usd": 0.5},
    {"id": "c", "project_id": "p", "request": "terceiro pedido com tamanho ok", "max_cost_usd": 0.5},
]


def _cases_file(tmp_path: Path) -> Path:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(CASES), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_budget_bounds_each_case_and_stops_the_suite(tmp_path: Path) -> None:
    seen: list[float] = []

    async def fake_runner(client, case, api_key):
        seen.append(case.max_cost_usd)
        return CaseResult(case_id=case.id, outcome="completed", completed=True, first_pass=True, cost_usd=0.4, elapsed_seconds=5)

    report = await run_evals(
        _cases_file(tmp_path), "k", budget_usd=0.7, policy=BenchmarkPolicy(), in_process=False,
        case_runner=fake_runner,
    )
    # caso a: teto 0.5; caso b: só restam 0.3; caso c: sem orçamento, não roda
    assert seen == [0.5, pytest.approx(0.3)]
    assert report.stopped_for_budget is True
    assert report.spent_usd == pytest.approx(0.8)
    outcomes = [r["outcome"] for r in report.summary["results"]]
    assert outcomes == ["completed", "completed", "budget_exhausted"]


@pytest.mark.asyncio
async def test_gate_and_reports(tmp_path: Path) -> None:
    async def fake_runner(client, case, api_key):
        return CaseResult(case_id=case.id, outcome="completed", completed=True, first_pass=case.id != "c", cost_usd=0.1, elapsed_seconds=3)

    policy = BenchmarkPolicy(min_completion_rate=1.0, min_first_pass_rate=0.6, max_average_cost_usd=0.2, max_p95_elapsed_seconds=10)
    report = await run_evals(_cases_file(tmp_path), "k", budget_usd=5, policy=policy, in_process=False, case_runner=fake_runner)
    assert report.gate_passed is True
    markdown = render_markdown(report)
    assert "Gate: **aprovado**" in markdown and "| c | completed | × |" in markdown

    json_path, md_path = write_reports(report, tmp_path / "out")
    assert json_path.exists() and md_path.exists()
    assert (tmp_path / "out" / "evals-latest.md").read_text(encoding="utf-8") == markdown
    assert json.loads((tmp_path / "out" / "evals-latest.json").read_text(encoding="utf-8"))["spent_usd"] == pytest.approx(0.3)

    strict = BenchmarkPolicy(min_first_pass_rate=0.9)
    failing = await run_evals(_cases_file(tmp_path), "k", budget_usd=5, policy=strict, in_process=False, case_runner=fake_runner)
    assert failing.gate_passed is False


def test_load_policy_defaults_and_file(tmp_path: Path) -> None:
    assert load_policy(None) == BenchmarkPolicy()
    gates = tmp_path / "gates.json"
    gates.write_text('{"min_completion_rate": 0.75, "max_average_cost_usd": 0.5}', encoding="utf-8")
    assert load_policy(gates).min_completion_rate == 0.75
    repo_gates = Path(__file__).resolve().parents[2] / "evals" / "gates.json"
    assert load_policy(repo_gates).max_average_cost_usd <= 1.0
