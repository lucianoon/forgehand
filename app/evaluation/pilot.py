from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx

from app.evaluation.benchmark import (
    BenchmarkCase,
    BenchmarkPolicy,
    _run_cases,
)


def expand_cases(cases: list[BenchmarkCase], rounds: int) -> list[BenchmarkCase]:
    return [
        case.model_copy(update={"id": f"{case.id}-run-{round_number}"})
        for round_number in range(1, rounds + 1)
        for case in cases
    ]


async def run_pilot(
    base_url: str,
    cases_path: Path,
    api_key: str,
    *,
    rounds: int = 3,
    concurrency: int = 1,
    case_ids: set[str] | None = None,
) -> dict[str, Any]:
    raw = json.loads(cases_path.read_text(encoding="utf-8"))
    base_cases = [BenchmarkCase.model_validate(item) for item in raw]
    if case_ids:
        base_cases = [case for case in base_cases if case.id in case_ids]
        missing = case_ids - {case.id for case in base_cases}
        if missing:
            raise ValueError(f"Casos não encontrados: {', '.join(sorted(missing))}")
    cases = expand_cases(base_cases, rounds)
    policy = BenchmarkPolicy(
        min_completion_rate=0.8,
        min_first_pass_rate=0.6,
        max_average_cost_usd=0.05,
        max_p95_elapsed_seconds=120,
    )
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        report = await _run_cases(client, cases, api_key, concurrency, policy)
    report["pilot"] = {
        "type": "internal_technical",
        "rounds": rounds,
        "scenarios": len(base_cases),
        "workflow_budget_cap_usd": sum(case.max_cost_usd for case in cases),
        "automatic_pull_requests": False,
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Piloto técnico Agent Forge")
    parser.add_argument("--base-url", default="http://localhost:8001")
    parser.add_argument("--cases", type=Path, default=Path("benchmarks/cases.json"))
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument(
        "--case-id",
        action="append",
        dest="case_ids",
        help="Executa apenas o caso informado; pode ser repetido.",
    )
    parser.add_argument("--output", type=Path, default=Path("reports/pilot.json"))
    parser.add_argument("--fail-on-gate", action="store_true")
    args = parser.parse_args()
    if args.rounds < 1 or args.concurrency < 1:
        raise SystemExit("--rounds e --concurrency devem ser >= 1.")
    api_key = os.getenv("AGENT_FORGE_API_KEY", "")
    if not api_key:
        raise SystemExit("Defina AGENT_FORGE_API_KEY.")
    report = asyncio.run(
        run_pilot(
            args.base_url,
            args.cases,
            api_key,
            rounds=args.rounds,
            concurrency=args.concurrency,
            case_ids=set(args.case_ids) if args.case_ids else None,
        )
    )
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if args.fail_on_gate and not report["quality_gate"]["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
