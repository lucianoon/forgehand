"""Explicit, budgeted qualification via the real factory API and Docker runner."""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import math
import os
import re
from contextlib import nullcontext
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from app.evaluation.benchmark import _percentile
from app.factory.build_strategy import BuildProfileRegistry
from app.factory.sandbox import DockerBuildRunner, DockerCLI
from app.factory.workspace import LocalGitWorkspaceManager
from app.models.build import BuildPhase, BuildPhaseName, BuildProfile
from app.infrastructure.scm import GitHubSCMClient
from app.models.build_execution import BuildOutcome
from app.models.factory import (
    BuildProfileSelection,
    DirectWorkOrderSource,
    RepositoryTarget,
    WorkOrder,
)


class FactoryCase(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]+$")
    ecosystem: str = Field(pattern=r"^(python|node)$")
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    base_ref: str = Field(default="main", min_length=1, max_length=255)
    request: str = Field(min_length=10)
    acceptance_criteria: list[str] = Field(min_length=1)
    expected_paths: list[str] = Field(min_length=1)
    hidden_case: str = Field(pattern=r"^[a-z]+$")
    max_cost_usd: float = Field(gt=0, allow_inf_nan=False)
    timeout_seconds: int = Field(gt=0)


class FactoryResult(BaseModel):
    case_id: str
    outcome: str = "not_started"
    workflow_id: str | None = None
    intake_key: str | None = None
    base_sha: str | None = None
    base_ref: str = "main"
    repository: str | None = None
    branch: str | None = None
    pull_request: int | None = None
    commit_sha: str | None = None
    ci: str | None = None
    hidden_check: bool = False
    scope_passed: bool = False
    changed_paths: list[str] = Field(default_factory=list)
    first_pass: bool = False
    intervention: bool = False
    isolation_violations: int = Field(default=0, ge=0)
    technical_failure: str | None = None
    workflow_error: str | None = None
    tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0, allow_inf_nan=False)
    reserved_unknown_cost_usd: float = Field(default=0, ge=0, allow_inf_nan=False)
    elapsed_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)

    @property
    def green(self) -> bool:
        return bool(
            self.outcome == "ready_for_human_review"
            and self.pull_request
            and self.commit_sha
            and self.ci == "success"
            and self.hidden_check
            and self.scope_passed
            and not self.technical_failure
        )


def summarize_factory(
    results: list[FactoryResult], *, sandbox_qualified: bool = False
) -> dict[str, Any]:
    total = len(results)
    green = sum(item.green for item in results)
    isolation = sum(item.isolation_violations for item in results)
    unclassified = sum(item.technical_failure == "unclassified" for item in results)
    checks = {
        "sandbox_preflight_passed": sandbox_qualified,
        "five_cases": total == 5 and len({r.case_id for r in results}) == 5,
        "four_green_prs": green >= 4,
        "zero_isolation_violations": isolation == 0,
        "zero_unclassified_technical_failures": unclassified == 0,
        "effects_reconciled": not any(
            r.technical_failure in {"intake_unconfirmed", "cancellation_unconfirmed"}
            for r in results
        ),
    }

    def rate(count: float) -> float:
        return count / total if total else 0.0

    return {
        "cases": total,
        "green_pr_rate": rate(green),
        "completion_rate": rate(
            sum(r.outcome == "ready_for_human_review" for r in results)
        ),
        "first_pass_rate": rate(sum(r.green and r.first_pass for r in results)),
        "intervention_rate": rate(sum(r.intervention for r in results)),
        "technical_failure_rate": rate(
            sum(r.technical_failure is not None for r in results)
        ),
        "isolation_violations": isolation,
        "tokens": sum(r.tokens for r in results),
        "cost_usd": sum(r.cost_usd for r in results),
        "reserved_unknown_cost_usd": sum(r.reserved_unknown_cost_usd for r in results),
        "mean_seconds": rate(sum(r.elapsed_seconds for r in results)),
        "p95_seconds": _percentile([r.elapsed_seconds for r in results], 0.95),
        "release_gate": {"passed": all(checks.values()), "checks": checks},
        "remote_artifacts": [
            {
                "repository": r.repository,
                "base_ref": r.base_ref,
                "base_sha": r.base_sha,
                "branch": r.branch,
                "pull_request": r.pull_request,
                "commit_sha": r.commit_sha,
            }
            for r in results
            if r.workflow_id
        ],
        "results": [r.model_dump(mode="json") for r in results],
    }


async def independent_check(
    case: FactoryCase,
    result: FactoryResult,
    fixture_root: Path,
    github: httpx.AsyncClient,
    socket: str,
) -> None:
    assert (
        result.repository
        and result.pull_request
        and result.commit_sha
        and result.branch
    )
    pull = await github.get(f"/repos/{result.repository}/pulls/{result.pull_request}")
    pull.raise_for_status()
    data = pull.json()
    if data["head"]["sha"] != result.commit_sha or data["head"]["ref"] != result.branch:
        raise ValueError("publication_identity_changed")
    scm = GitHubSCMClient(
        token=github.headers.get("Authorization", "").removeprefix("Bearer "),
        client=github,
    )
    result.ci = (await scm.fetch_checks(result.repository, result.commit_sha)).state
    if result.ci != "success":
        return
    paths: list[str] = []
    for page in range(1, 31):
        response = await github.get(
            f"/repos/{result.repository}/pulls/{result.pull_request}/files",
            params={"per_page": 100, "page": page},
        )
        response.raise_for_status()
        batch = response.json()
        paths.extend(item["filename"] for item in batch)
        paths.extend(
            item["previous_filename"] for item in batch if item.get("previous_filename")
        )
        if len(batch) < 100:
            break
    else:
        raise ValueError("diff_inventory_limit")
    result.changed_paths = sorted(set(paths))
    result.scope_passed = bool(paths) and all(
        any(fnmatch.fnmatchcase(p, scope) for scope in case.expected_paths)
        for p in paths
    )
    if not result.scope_passed:
        return
    profiles = json.loads((fixture_root / "profiles.json").read_text())
    profile = BuildProfile.model_validate(profiles[f"{case.ecosystem}-fixture"])
    extension = "py" if case.ecosystem == "python" else "cjs"
    script_name = f"__forgehand_verify.{extension}"
    script = (fixture_root / "hidden" / f"{case.ecosystem}.{extension}").read_text()
    # Fresh clone of the *published commit*, never the agent's residual checkout.
    root = Path(tempfile.mkdtemp(prefix="forgehand-independent-"))
    manager = LocalGitWorkspaceManager(root, approved_hosts=["github.com"])
    order = WorkOrder(
        source=DirectWorkOrderSource(),
        repository=RepositoryTarget(
            full_name=result.repository,
            base_ref=result.branch,
            expected_base_sha=result.commit_sha,
        ),
        requested_outcome=case.request,
        acceptance_criteria=case.acceptance_criteria,
    )
    lease = await manager.provision(str(uuid4()), order)
    try:
        script_path = Path(lease.local_path) / script_name
        if script_path.exists() or script_path.is_symlink():
            raise ValueError("reserved_verifier_path")
        script_path.write_text(script)
        verification = profile.model_copy(
            update={
                "phases": (
                    BuildPhase(
                        name=BuildPhaseName.TEST,
                        argv=(
                            f"/usr/local/bin/{'python' if case.ecosystem == 'python' else 'node'}",
                            script_name,
                            case.hidden_case,
                        ),
                    ),
                )
            }
        )
        runner = DockerBuildRunner(
            BuildProfileRegistry({verification.name: verification}),
            DockerCLI(socket_path=socket),
            journal=manager.journal,
        )
        selection = BuildProfileSelection(
            selected_profile=verification.name,
            selection_reason="explicit",
            phases=["test"],
            profile_digest=verification.fingerprint(),
        )
        evidence = await runner.run(lease, selection)
        result.hidden_check = evidence.outcome == BuildOutcome.SUCCESS
        if evidence.outcome in {
            BuildOutcome.INFRASTRUCTURE_ERROR,
            BuildOutcome.POLICY_REJECTION,
        }:
            result.technical_failure = evidence.error_code or "sandbox"
        if runner.active_containers:
            result.technical_failure = "sandbox_cleanup_pending"
            return
    finally:
        if not manager.journal.containers():
            await manager.cleanup(lease)


async def run_factory_case(
    client: httpx.AsyncClient,
    github: httpx.AsyncClient,
    case: FactoryCase,
    repository: str,
    api_key: str,
    fixture_root: Path,
    socket: str,
    run_id: str,
) -> FactoryResult:
    started = time.monotonic()
    result = FactoryResult(
        case_id=case.id, repository=repository, base_sha=case.base_sha,
        base_ref=case.base_ref,
    )
    result.intake_key = f"qualification:{run_id}:{case.id}"
    headers = {"X-API-Key": api_key}
    terminal = {"ready_for_human_review", "failed", "cancelled", "awaiting_decision"}
    state: dict[str, Any] = {}
    try:
        created = await client.post(
            "/workflows",
            headers=headers,
            json={
                "project_id": "factory-qualification",
                "work_order": {
                    "repository": repository,
                    "base_ref": case.base_ref,
                    "expected_base_sha": case.base_sha,
                    "requested_outcome": case.request,
                    "acceptance_criteria": case.acceptance_criteria,
                    "build_profile": f"{case.ecosystem}-fixture",
                    "idempotency_key": result.intake_key,
                    "limits": {
                        "max_cost_usd": case.max_cost_usd,
                        "max_wall_clock_seconds": case.timeout_seconds,
                        "max_iterations": 3,
                    },
                    "delivery_policy": {
                        "wait_for_checks": True,
                        "checks_timeout_seconds": min(
                            7200, max(30, case.timeout_seconds)
                        ),
                    },
                },
            },
        )
        created.raise_for_status()
        result.workflow_id = created.json()["workflow_id"]
        result.branch = f"forgehand/{result.workflow_id}"
        while time.monotonic() - started < case.timeout_seconds:
            response = await client.get(
                f"/workflows/{result.workflow_id}", headers=headers
            )
            response.raise_for_status()
            state = response.json()
            usage = state.get("usage") or {}
            result.cost_usd = float(usage.get("cost_usd", 0))
            result.tokens = int(usage.get("tokens", 0))
            delivery = state.get("delivery") or {}
            result.pull_request = delivery.get("pull_request_number")
            result.commit_sha = delivery.get("commit_sha")
            result.ci = delivery.get("ci_state")
            result.outcome = state["status"]
            workspace = state.get("workspace") or {}
            if workspace.get("base_sha") and workspace["base_sha"] != case.base_sha:
                result.isolation_violations += 1
                result.technical_failure = "base_sha_mismatch"
                break
            if result.outcome in terminal:
                result.intervention = result.outcome == "awaiting_decision"
                result.first_pass = (
                    bool(state.get("tasks"))
                    and all(t.get("attempts") == 1 for t in state["tasks"])
                    and delivery.get("attempts") == 1
                )
                if result.outcome == "failed":
                    result.technical_failure = "workflow_failed"
                    error = state.get("error")
                    if isinstance(error, str) and re.fullmatch(
                        r"(?:RetryableProviderError|NonRetryableProviderError|CircuitOpenError|StructuredOutputError)"
                        r"(?::(?:HTTP[45][0-9]{2}|ReadTimeout|WriteTimeout|ConnectTimeout|PoolTimeout|"
                        r"ConnectError|ReadError|WriteError|RemoteProtocolError))?",
                        error,
                    ):
                        result.workflow_error = error
                break
            if result.cost_usd >= case.max_cost_usd:
                result.outcome = "budget_exhausted"
                break
            await asyncio.sleep(0.5)
        else:
            result.outcome = "timeout"
            result.technical_failure = "timeout"
        if result.outcome == "ready_for_human_review":
            await independent_check(case, result, fixture_root, github, socket)
    except httpx.HTTPError:
        result.technical_failure = (
            "http_error" if result.workflow_id else "intake_unconfirmed"
        )
        if result.workflow_id is None:
            result.reserved_unknown_cost_usd = case.max_cost_usd
        result.outcome = "failed"
    except Exception:
        result.technical_failure = "unclassified"
        result.outcome = "failed"
    finally:
        if result.workflow_id and state.get("status") not in {
            "ready_for_human_review",
            "failed",
            "cancelled",
        }:
            try:
                cancellation = await client.post(
                    f"/workflows/{result.workflow_id}/cancel", headers=headers
                )
                cancellation.raise_for_status()
            except httpx.HTTPError:
                result.technical_failure = "cancellation_unconfirmed"
        result.elapsed_seconds = time.monotonic() - started
    return FactoryResult.model_validate(result.model_dump())


async def run_qualification(
    client: httpx.AsyncClient,
    github: httpx.AsyncClient,
    cases: list[FactoryCase],
    repositories: dict[str, str],
    api_key: str,
    fixture_root: Path,
    socket: str,
    total_budget: float,
    sandbox_qualified: bool = False,
) -> dict[str, Any]:
    if not math.isfinite(total_budget) or total_budget <= 0:
        raise ValueError("positive finite budget required")
    results: list[FactoryResult] = []
    run_id = str(uuid4())
    for case in cases:
        if any(
            r.technical_failure in {"intake_unconfirmed", "cancellation_unconfirmed"}
            for r in results
        ):
            results.append(
                FactoryResult(case_id=case.id, outcome="blocked_unconfirmed_effects")
            )
            continue
        remaining = total_budget - sum(
            r.cost_usd + r.reserved_unknown_cost_usd for r in results
        )
        if case.max_cost_usd > remaining:
            results.append(FactoryResult(case_id=case.id, outcome="budget_exhausted"))
            continue
        result = await run_factory_case(
            client,
            github,
            case,
            repositories[case.ecosystem],
            api_key,
            fixture_root,
            socket,
            run_id,
        )
        results.append(result)
    return summarize_factory(results, sandbox_qualified=sandbox_qualified)


async def sandbox_preflight(fixture_root: Path, socket: str) -> bool:
    """Independent host-boundary probe; no model calls or repository code."""
    profiles = json.loads((fixture_root / "profiles.json").read_text())
    for ecosystem in ("python", "node"):
        # Explicit cleanup only after confirmed container termination.
        with nullcontext(tempfile.mkdtemp(prefix="forgehand-isolation-")) as directory:
            root = Path(directory)
            if ecosystem == "python":
                script_name = "probe.py"
                source = "import os,socket,pathlib\nassert os.getuid()!=0\nassert not any(k in os.environ for k in ['GITHUB_TOKEN','OPENROUTER_API_KEY','OPENAI_API_KEY'])\nsocket.setdefaulttimeout(1)\ntry:\n socket.create_connection(('1.1.1.1',443))\nexcept OSError:\n pass\nelse:\n raise AssertionError('network available')\ntry:\n pathlib.Path('/outside').touch()\nexcept OSError:\n pass\nelse:\n raise AssertionError('root writable')\n"
            else:
                script_name = "probe.cjs"
                source = "const a=require('node:assert/strict'), fs=require('node:fs'); a.notEqual(process.getuid(),0); for(const k of ['GITHUB_TOKEN','OPENROUTER_API_KEY','OPENAI_API_KEY']) a.equal(process.env[k],undefined); a.throws(()=>fs.writeFileSync('/outside','x')); a.equal(fs.readFileSync('/proc/net/route','utf8').trim().split('\\n').length,1);"
            marker = (
                "\nprint('version-1')\n"
                if ecosystem == "python"
                else "\nconsole.log('version-1');\n"
            )
            (root / script_name).write_text(source + marker)
            profile = BuildProfile.model_validate(profiles[f"{ecosystem}-fixture"])
            probe = profile.model_copy(
                update={
                    "phases": (
                        BuildPhase(
                            name=BuildPhaseName.TEST,
                            argv=(
                                f"/usr/local/bin/{'python' if ecosystem == 'python' else 'node'}",
                                script_name,
                            ),
                        ),
                    )
                }
            )
            runner = DockerBuildRunner(
                BuildProfileRegistry({probe.name: probe}), DockerCLI(socket_path=socket)
            )
            from app.models.factory import WorkspaceLease, WorkspaceLifecycle

            lease = WorkspaceLease(
                workflow_id=f"preflight-{uuid4()}",
                repository=RepositoryTarget(full_name="fixture/preflight"),
                local_path=str(root),
                branch="forgehand/preflight",
                base_sha="a" * 40,
                state=WorkspaceLifecycle.ACTIVE,
            )
            selection = BuildProfileSelection(
                selected_profile=probe.name,
                selection_reason="explicit",
                phases=["test"],
                profile_digest=probe.fingerprint(),
            )
            report = await runner.run(lease, selection)
            if report.outcome != BuildOutcome.SUCCESS or runner.active_containers:
                # Do not authorize a paid run after an unproven boundary.
                return False
            # A fresh container must observe host edits from the previous iteration.
            # Some Desktop file-sharing backends can otherwise validate stale code.
            (root / script_name).write_text(
                source + marker.replace("version-1", "version-2")
            )
            updated = await runner.run(lease, selection)
            if (
                updated.outcome != BuildOutcome.SUCCESS
                or runner.active_containers
                or len(updated.phases) != 1
                or updated.phases[0].stdout.strip() != "version-2"
            ):
                return False
            shutil.rmtree(root)
    return True


async def main_async(args: argparse.Namespace) -> int:
    if not args.allow_live:
        raise ValueError("Explicit --allow-live required: LLM costs and remote PRs")
    api_key = os.environ.get("FORGEHAND_API_KEY", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not api_key or not github_token:
        raise ValueError("FORGEHAND_API_KEY and GITHUB_TOKEN required")
    cases = [
        FactoryCase.model_validate(
            {**value, **({"base_ref": args.base_ref} if getattr(args, "base_ref", None) else {})}
        )
        for value in json.loads((args.fixtures / "cases.json").read_text())
    ]
    repositories = {"python": args.python_repository, "node": args.node_repository}
    for repository in repositories.values():
        RepositoryTarget(full_name=repository)
    qualified = await sandbox_preflight(args.fixtures, args.socket)
    if not qualified:
        raise ValueError("Sandbox preflight failed; no paid cases started")
    async with (
        httpx.AsyncClient(base_url=args.api_url, timeout=30) as client,
        httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=30,
        ) as github,
    ):
        report = await run_qualification(
            client,
            github,
            cases,
            repositories,
            api_key,
            args.fixtures,
            args.socket,
            args.total_budget,
            sandbox_qualified=qualified,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    args.output.with_suffix(".md").write_text(
        f"# Factory qualification\n\nRelease gate: {'PASS' if report['release_gate']['passed'] else 'FAIL'}\n\nGreen PR rate: {report['green_pr_rate']:.0%}\n\nCost: USD {report['cost_usd']:.4f}\n\nRemote artifacts (preserved):\n\n```json\n{json.dumps(report['remote_artifacts'], indent=2)}\n```\n"
    )
    return 0 if report["release_gate"]["passed"] else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--python-repository", required=True)
    parser.add_argument("--node-repository", required=True)
    parser.add_argument("--base-ref", help="Fixture branch pinned to the manifest SHA (default: main)")
    parser.add_argument("--total-budget", type=float, required=True)
    parser.add_argument("--fixtures", type=Path, default=Path("benchmarks/factory"))
    parser.add_argument("--socket", default="/var/run/docker.sock")
    parser.add_argument(
        "--output", type=Path, default=Path("reports/factory-qualification.json")
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
