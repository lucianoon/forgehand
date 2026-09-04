"""Avaliação contínua com LLM real, orçamento fechado e gates.

Reaproveita o benchmark (casos, run_case, summarize, política) e acrescenta o
que a CI precisa: orçamento total em dólares que interrompe a suíte antes de
estourar, relatório JSON + Markdown reproduzível e código de saída pelo gate.

    uv run python -m app.evaluation.evals --budget-usd 1.5 --gates evals/gates.json

Os casos vivem em evals/cases.json; a linha de base publicada, em evals/baseline/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from app.evaluation.benchmark import (
    BenchmarkCase,
    BenchmarkPolicy,
    CaseResult,
    _load_cases,
    run_case,
    summarize,
)

CaseRunner = Callable[[httpx.AsyncClient, BenchmarkCase, str], Awaitable[CaseResult]]


class EvalReport(BaseModel):
    started_at: str
    finished_at: str
    budget_usd: float
    spent_usd: float
    stopped_for_budget: bool
    provider: str
    summary: dict[str, Any]

    @property
    def gate_passed(self) -> bool:
        return bool(self.summary.get("quality_gate", {}).get("passed"))


@asynccontextmanager
async def _open_client(in_process: bool, base_url: str) -> AsyncIterator[httpx.AsyncClient]:
    if not in_process:
        async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
            yield client
        return
    from app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://forgehand", timeout=30
        ) as client:
            yield client


async def run_evals(
    cases_path: Path,
    api_key: str,
    *,
    budget_usd: float,
    policy: BenchmarkPolicy,
    in_process: bool = True,
    base_url: str = "http://localhost:8000",
    case_ids: set[str] | None = None,
    case_runner: CaseRunner = run_case,
) -> EvalReport:
    """Casos em sequência: o teto de cada um é limitado ao que resta do
    orçamento total; sem orçamento, os restantes são marcados, não rodados."""
    cases = _load_cases(cases_path, case_ids)
    started = datetime.now(timezone.utc)
    results: list[CaseResult] = []
    spent = 0.0
    stopped = False
    async with _open_client(in_process, base_url) as client:
        for case in cases:
            remaining = round(budget_usd - spent, 6)
            if remaining <= 0.01:
                stopped = True
                results.append(
                    CaseResult(case_id=case.id, outcome="budget_exhausted", error="budget")
                )
                continue
            bounded = case.model_copy(
                update={"max_cost_usd": min(case.max_cost_usd, remaining)}
            )
            result = await case_runner(client, bounded, api_key)
            spent += result.cost_usd
            results.append(result)
    return EvalReport(
        started_at=started.isoformat(),
        finished_at=datetime.now(timezone.utc).isoformat(),
        budget_usd=budget_usd,
        spent_usd=round(spent, 6),
        stopped_for_budget=stopped,
        provider=os.getenv("LLM_PROVIDER_BACKEND", "anthropic"),
        summary=summarize(results, policy),
    )


def render_markdown(report: EvalReport) -> str:
    summary = report.summary
    gate = summary.get("quality_gate", {})
    checks = gate.get("checks", {})
    policy = gate.get("policy", {})
    lines = [
        "# Evals Forgehand",
        "",
        f"- Início: {report.started_at}",
        f"- Provider: `{report.provider}`",
        f"- Orçamento: US$ {report.budget_usd:.2f} · gasto: US$ {report.spent_usd:.4f}"
        + (" · **interrompido por orçamento**" if report.stopped_for_budget else ""),
        f"- Gate: **{'aprovado' if gate.get('passed') else 'reprovado'}**",
        "",
        "| Métrica | Valor | Limite | Ok |",
        "|---|---:|---:|:-:|",
        f"| Conclusão | {summary.get('completion_rate', 0):.0%} | >= {policy.get('min_completion_rate', 0):.0%} | {'✓' if checks.get('completion_rate') else '×'} |",
        f"| First pass | {summary.get('first_pass_rate', 0):.0%} | >= {policy.get('min_first_pass_rate', 0):.0%} | {'✓' if checks.get('first_pass_rate') else '×'} |",
        f"| Custo médio | US$ {summary.get('average_cost_usd', 0):.4f} | <= US$ {policy.get('max_average_cost_usd', 0):.2f} | {'✓' if checks.get('average_cost_usd') else '×'} |",
        f"| Latência p95 | {summary.get('p95_elapsed_seconds', 0):.1f} s | <= {policy.get('max_p95_elapsed_seconds', 0):.0f} s | {'✓' if checks.get('p95_elapsed_seconds') else '×'} |",
        "",
        "| Caso | Resultado | First pass | Custo | Tempo |",
        "|---|---|:-:|---:|---:|",
    ]
    for item in summary.get("results", []):
        lines.append(
            f"| {item['case_id']} | {item['outcome']} | {'✓' if item.get('first_pass') else '×'} "
            f"| US$ {item.get('cost_usd', 0):.4f} | {item.get('elapsed_seconds', 0):.0f} s |"
        )
    return "\n".join(lines) + "\n"


def write_reports(report: EvalReport, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.started_at[:19].replace(":", "").replace("-", "")
    json_path = output_dir / f"evals-{stamp}.json"
    md_path = output_dir / f"evals-{stamp}.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    (output_dir / "evals-latest.json").write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    (output_dir / "evals-latest.md").write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")
    return json_path, md_path


def load_policy(path: Path | None) -> BenchmarkPolicy:
    if path is None or not path.exists():
        return BenchmarkPolicy()
    return BenchmarkPolicy.model_validate(json.loads(path.read_text(encoding="utf-8")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evals do Forgehand com LLM real e orçamento fechado")
    parser.add_argument("--cases", type=Path, default=Path("evals/cases.json"))
    parser.add_argument("--gates", type=Path, default=Path("evals/gates.json"))
    parser.add_argument("--budget-usd", type=float, default=1.0)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--base-url", default=None, help="Usa uma API já em execução em vez de subir em processo")
    parser.add_argument("--api-key", default=os.getenv("FORGEHAND_API_KEY", "dev-key"))
    args = parser.parse_args(argv)

    report = asyncio.run(
        run_evals(
            args.cases,
            args.api_key,
            budget_usd=args.budget_usd,
            policy=load_policy(args.gates),
            in_process=args.base_url is None,
            base_url=args.base_url or "http://localhost:8000",
            case_ids=set(args.case_id) or None,
        )
    )
    json_path, md_path = write_reports(report, args.output_dir)
    # Consoles Windows em cp1252 não codificam ✓/×; o relatório já está em disco.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.write(render_markdown(report))
    sys.stdout.write(f"\nRelatórios: {json_path} · {md_path}\n")
    return 0 if report.gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
