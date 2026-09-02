from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field


class BenchmarkCase(BaseModel):
    id: str
    project_id: str
    request: str = Field(min_length=10)
    acceptance_criteria: list[str] = Field(default_factory=list)
    max_cost_usd: float = Field(default=1.0, gt=0)
    timeout_seconds: float = Field(default=300, gt=0)
    # Entrega (PR + CI) — mesmo shape de `delivery` em POST /workflows.
    delivery: dict[str, Any] | None = None


class CaseResult(BaseModel):
    case_id: str
    workflow_id: str | None = None
    outcome: str = "unknown"
    completed: bool = False
    first_pass: bool = False
    cost_usd: float = 0.0
    tokens: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None
    diagnostics: dict[str, Any] | None = None
    delivery: dict[str, Any] | None = None


class BenchmarkPolicy(BaseModel):
    min_completion_rate: float = Field(default=0.8, ge=0, le=1)
    min_first_pass_rate: float = Field(default=0.6, ge=0, le=1)
    max_average_cost_usd: float = Field(default=1.0, gt=0)
    max_p95_elapsed_seconds: float = Field(default=300, gt=0)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * percentile + 0.999) - 1))
    return ordered[index]


def summarize(
    results: list[CaseResult], policy: BenchmarkPolicy | None = None
) -> dict[str, Any]:
    policy = policy or BenchmarkPolicy()
    total = len(results)
    completed = sum(result.completed for result in results)
    first_pass = sum(result.first_pass for result in results)
    completion_rate = completed / total if total else 0.0
    first_pass_rate = first_pass / total if total else 0.0
    average_cost = sum(result.cost_usd for result in results) / total if total else 0.0
    p95_elapsed = _percentile([result.elapsed_seconds for result in results], 0.95)
    completed_cost = sum(result.cost_usd for result in results if result.completed)
    human_interventions = sum(
        result.outcome == "awaiting_decision" for result in results
    )
    technical_failures = sum(
        result.outcome in {"failed", "cancelled", "timeout", "create_failed"}
        for result in results
    )
    checks = {
        "completion_rate": completion_rate >= policy.min_completion_rate,
        "first_pass_rate": first_pass_rate >= policy.min_first_pass_rate,
        "average_cost_usd": average_cost <= policy.max_average_cost_usd,
        "p95_elapsed_seconds": p95_elapsed <= policy.max_p95_elapsed_seconds,
    }
    return {
        "cases": total,
        "completion_rate": completion_rate,
        "first_pass_rate": first_pass_rate,
        "total_cost_usd": sum(result.cost_usd for result in results),
        "average_cost_usd": average_cost,
        "average_elapsed_seconds": (
            sum(result.elapsed_seconds for result in results) / total if total else 0.0
        ),
        "p95_elapsed_seconds": p95_elapsed,
        "timeouts": sum(result.error == "timeout" for result in results),
        "human_intervention_rate": human_interventions / total if total else 0.0,
        "technical_failure_rate": technical_failures / total if total else 0.0,
        "cost_per_completed_usd": completed_cost / completed if completed else 0.0,
        "quality_gate": {
            "passed": all(checks.values()),
            "checks": checks,
            "policy": policy.model_dump(mode="json"),
        },
        "results": [result.model_dump(mode="json") for result in results],
    }


async def run_case(
    client: httpx.AsyncClient, case: BenchmarkCase, api_key: str
) -> CaseResult:
    started = time.monotonic()
    headers = {"X-API-Key": api_key}
    created = await client.post(
        "/workflows",
        headers=headers,
        json={
            "project_id": case.project_id,
            "request": case.request,
            "acceptance_criteria": case.acceptance_criteria,
            "budget": {"max_cost_usd": case.max_cost_usd},
            **({"delivery": case.delivery} if case.delivery else {}),
        },
    )
    if created.status_code != 202:
        return CaseResult(
            case_id=case.id,
            outcome="create_failed",
            elapsed_seconds=time.monotonic() - started,
            error=f"create:{created.status_code}",
        )
    workflow_id = created.json()["workflow_id"]
    deadline = time.monotonic() + case.timeout_seconds
    while time.monotonic() < deadline:
        response = await client.get(f"/workflows/{workflow_id}", headers=headers)
        if response.status_code != 200:
            await asyncio.sleep(0.5)
            continue
        state = response.json()
        if state["status"] in {
            "completed",
            "failed",
            "cancelled",
            "awaiting_decision",
        }:
            tasks = state.get("tasks") or []
            usage = state.get("usage") or {}
            diagnostics = None
            if state["status"] != "completed":
                details_response = await client.get(
                    f"/workflows/{workflow_id}/details", headers=headers
                )
                diagnostics = {
                    "pending_decision": state.get("pending_decision"),
                    "tasks": tasks,
                    "details": (
                        details_response.json()
                        if details_response.status_code == 200
                        else None
                    ),
                }
            return CaseResult(
                case_id=case.id,
                workflow_id=workflow_id,
                outcome=state["status"],
                completed=state["status"] == "completed",
                first_pass=bool(tasks)
                and all(task.get("attempts") == 1 for task in tasks),
                cost_usd=float(usage.get("cost_usd", 0)),
                tokens=int(usage.get("tokens", 0)),
                elapsed_seconds=time.monotonic() - started,
                error=(
                    state.get("error")
                    or (
                        (state.get("pending_decision") or {}).get("reason")
                        if state["status"] == "awaiting_decision"
                        else None
                    )
                ),
                diagnostics=diagnostics,
                delivery=state.get("delivery"),
            )
        await asyncio.sleep(0.5)
    try:
        await client.post(f"/workflows/{workflow_id}/cancel", headers=headers)
    except httpx.HTTPError:
        pass
    return CaseResult(
        case_id=case.id,
        workflow_id=workflow_id,
        outcome="timeout",
        elapsed_seconds=time.monotonic() - started,
        error="timeout",
    )


async def run_benchmark(
    base_url: str,
    cases_path: Path,
    api_key: str,
    *,
    concurrency: int = 1,
    policy: BenchmarkPolicy | None = None,
    case_ids: set[str] | None = None,
) -> dict[str, Any]:
    cases = _load_cases(cases_path, case_ids)
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        return await _run_cases(client, cases, api_key, concurrency, policy)


async def _run_cases(
    client: httpx.AsyncClient,
    cases: list[BenchmarkCase],
    api_key: str,
    concurrency: int,
    policy: BenchmarkPolicy | None,
) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(concurrency)

    async def limited(case: BenchmarkCase) -> CaseResult:
        async with semaphore:
            return await run_case(client, case, api_key)

    results = await asyncio.gather(*(limited(case) for case in cases))
    return summarize(results, policy)


async def run_benchmark_in_process(
    cases_path: Path,
    api_key: str,
    *,
    concurrency: int = 1,
    policy: BenchmarkPolicy | None = None,
    case_ids: set[str] | None = None,
) -> dict[str, Any]:
    from app.main import create_app

    cases = _load_cases(cases_path, case_ids)
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://forgehand",
            timeout=30,
        ) as client:
            return await _run_cases(client, cases, api_key, concurrency, policy)


def _load_cases(cases_path: Path, case_ids: set[str] | None) -> list[BenchmarkCase]:
    raw = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = [BenchmarkCase.model_validate(item) for item in raw]
    if case_ids:
        cases = [case for case in cases if case.id in case_ids]
    if not cases:
        raise ValueError("Nenhum caso de benchmark selecionado.")
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Forgehand")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--in-process", action="store_true")
    parser.add_argument("--cases", type=Path, default=Path("benchmarks/cases.json"))
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--min-completion-rate", type=float, default=0.8)
    parser.add_argument("--min-first-pass-rate", type=float, default=0.6)
    parser.add_argument("--max-average-cost-usd", type=float, default=1.0)
    parser.add_argument("--max-p95-seconds", type=float, default=300)
    parser.add_argument("--fail-on-gate", action="store_true")
    args = parser.parse_args()
    api_key = os.getenv("FORGEHAND_API_KEY", "")
    if not api_key:
        raise SystemExit("Defina FORGEHAND_API_KEY.")
    if args.concurrency < 1:
        raise SystemExit("--concurrency deve ser >= 1.")
    policy = BenchmarkPolicy(
        min_completion_rate=args.min_completion_rate,
        min_first_pass_rate=args.min_first_pass_rate,
        max_average_cost_usd=args.max_average_cost_usd,
        max_p95_elapsed_seconds=args.max_p95_seconds,
    )
    if args.in_process:
        report = asyncio.run(
            run_benchmark_in_process(
                args.cases,
                api_key,
                concurrency=args.concurrency,
                policy=policy,
                case_ids=set(args.case_id) or None,
            )
        )
    else:
        report = asyncio.run(
            run_benchmark(
                args.base_url,
                args.cases,
                api_key,
                concurrency=args.concurrency,
                policy=policy,
                case_ids=set(args.case_id) or None,
            )
        )
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if args.fail_on_gate and not report["quality_gate"]["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
