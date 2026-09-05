"""Independent qualification against real, disposable Git history; no remote calls."""

import copy
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.evaluation import factory_qualification as qualification
from app.evaluation.factory_fixtures import prepare_fixture
from app.evaluation.factory_qualification import FactoryCase, FactoryResult
from app.factory.workspace import LocalGitWorkspaceManager
from app.models.build_execution import BuildOutcome, BuildPhaseResult, BuildRunResult

FIXTURES = Path(__file__).resolve().parents[2] / "benchmarks/factory"
MARKER = "FORGEHAND_VERIFIER_OK"


def git(root, *args):
    return subprocess.check_output(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            *args,
        ],
        cwd=root,
        env={
            "PATH": os.defpath,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        },
        text=True,
        stderr=subprocess.PIPE,
        timeout=30,
    ).strip()


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    repository, base = prepare_fixture("python", tmp_path, FIXTURES)
    case = FactoryCase.model_validate(
        json.loads((FIXTURES / "cases.json").read_text())[0]
    )
    assert base == case.base_sha
    git(repository, "checkout", "-b", "forgehand/wf")
    path = repository / "orders.py"
    path.write_text(
        path.read_text().replace(
            "round(sum(prices), 2)", "round(sum(prices) * (1-discount), 2)"
        )
    )
    git(repository, "add", ".")
    git(repository, "commit", "-m", "reference fix")
    head = git(repository, "rev-parse", "HEAD")
    result = FactoryResult(
        case_id=case.id,
        repository="fixture/python",
        base_sha=base,
        base_ref="main",
        branch="forgehand/wf",
        commit_sha=head,
        pull_request=7,
        outcome="ready_for_human_review",
    )
    pull = {
        "number": 7,
        "state": "open",
        "merged": False,
        "head": {
            "sha": head,
            "ref": result.branch,
            "repo": {"full_name": result.repository},
        },
        "base": {"sha": base, "ref": "main", "repo": {"full_name": result.repository}},
    }
    state = SimpleNamespace(
        case=case,
        result=result,
        repository=repository,
        pull=pull,
        final_pull=None,
        pulls=0,
        builds=0,
        ci_reads=0,
        managers=[],
        conclusion="success",
        final_conclusion=None,
        stdout=MARKER + "\n",
        output_truncated=False,
        phase_count=1,
        during_build=None,
    )

    def manager(root, **kwargs):
        instance = LocalGitWorkspaceManager(
            root,
            approved_hosts=[],
            allow_local_repositories=True,
            repository_url_resolver=lambda target: str(repository),
        )
        state.managers.append(instance)
        return instance

    class Runner:
        active_containers = ()

        def __init__(self, *args, **kwargs):
            pass

        async def run(self, lease, selection):
            state.builds += 1
            assert (
                git(Path(lease.local_path), "rev-parse", "HEAD")
                == state.result.commit_sha
            )
            assert (Path(lease.local_path) / "__forgehand_verify.py").is_file()
            if state.during_build:
                state.during_build()
            phase = BuildPhaseResult(
                phase="test",
                outcome=BuildOutcome.SUCCESS,
                command=("python", "verify.py"),
                image="test",
                cwd="/workspace",
                duration_seconds=0,
                exit_code=0,
                stdout=state.stdout,
                output_truncated=state.output_truncated,
            )
            return BuildRunResult(
                profile_name=selection.selected_profile,
                profile_digest=selection.profile_digest,
                outcome=BuildOutcome.SUCCESS,
                phases=(phase,) * state.phase_count,
            )

    monkeypatch.setattr(qualification, "LocalGitWorkspaceManager", manager)
    monkeypatch.setattr(qualification, "DockerBuildRunner", Runner)

    def handler(request):
        if request.url.path.endswith("/pulls/7"):
            state.pulls += 1
            return httpx.Response(
                200,
                json=(state.final_pull or state.pull)
                if state.pulls > 1
                else state.pull,
            )
        if request.url.path.endswith("/check-runs"):
            state.ci_reads += 1
            conclusion = (
                state.final_conclusion
                if state.ci_reads > 1 and state.final_conclusion
                else state.conclusion
            )
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "check_runs": [
                        {
                            "id": 1,
                            "name": "tests",
                            "status": "completed",
                            "conclusion": conclusion,
                        }
                    ],
                },
            )
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"total_count": 0, "statuses": []})
        if request.url.path.endswith("/annotations"):
            return httpx.Response(200, json=[])
        # Simulate a stale PR inventory claiming only permitted paths changed.
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=[{"filename": "orders.py"}])
        raise AssertionError(f"unexpected request: {request.url}")

    state.handler = handler
    return state


async def verify(state):
    async with httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(state.handler),
        headers={"Authorization": "Bearer test-only"},
    ) as client:
        await qualification.independent_check(
            state.case, state.result, FIXTURES, client, "/unused"
        )


@pytest.mark.asyncio
async def test_verifies_published_git_diff_and_records_verifier_identity(scenario):
    await verify(scenario)
    assert scenario.result.green
    assert scenario.result.changed_paths == ["orders.py"]
    assert scenario.result.verified_commit_sha == scenario.result.commit_sha
    assert len(scenario.result.verifier_sha256) == 64
    assert len(scenario.result.verification_profile_digest) == 64
    assert scenario.pulls == scenario.ci_reads == 2
    assert scenario.builds == 1
    assert all(not manager._root.exists() for manager in scenario.managers)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["base_ref", "base_sha", "base_repository", "head_repository", "closed", "merged"],
)
async def test_rejects_wrong_publication_before_running_code(scenario, field):
    if field == "base_ref":
        scenario.pull["base"]["ref"] = "unapproved"
    elif field == "base_sha":
        scenario.pull["base"]["sha"] = "c" * 40
    elif field == "base_repository":
        scenario.pull["base"]["repo"]["full_name"] = "other/repo"
    elif field == "head_repository":
        scenario.pull["head"]["repo"]["full_name"] = "other/repo"
    elif field == "closed":
        scenario.pull["state"] = "closed"
    else:
        scenario.pull["merged"] = True
    with pytest.raises(ValueError, match="publication_identity_changed"):
        await verify(scenario)
    assert scenario.builds == 0
    assert not scenario.result.green


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["head", "base", "closed"])
async def test_publication_drift_during_build_cannot_pass(scenario, drift):
    scenario.final_pull = copy.deepcopy(scenario.pull)
    if drift == "closed":
        scenario.final_pull["state"] = "closed"
    else:
        scenario.final_pull[drift]["sha"] = "d" * 40
    with pytest.raises(ValueError, match="publication_identity_changed"):
        await verify(scenario)
    assert not scenario.result.hidden_check
    assert not scenario.result.green
    assert scenario.builds == 1


@pytest.mark.asyncio
async def test_uses_immutable_diff_even_if_pr_inventory_claims_allowed_paths(scenario):
    git(scenario.repository, "mv", "config.json", "tests/config.json")
    git(scenario.repository, "commit", "-m", "rename out of scope")
    head = git(scenario.repository, "rev-parse", "HEAD")
    scenario.result.commit_sha = scenario.pull["head"]["sha"] = head
    await verify(scenario)
    assert not scenario.result.scope_passed
    assert scenario.result.changed_paths == [
        "config.json",
        "orders.py",
        "tests/config.json",
    ]
    assert scenario.builds == 0
    assert not scenario.result.green


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stdout,truncated,phase_count",
    [
        ("", False, 1),
        ("partial checks\n", False, 1),
        (MARKER + "\n", True, 1),
        (MARKER + "\n", False, 0),
    ],
)
async def test_exit_zero_without_complete_verifier_evidence_is_not_success(
    scenario, stdout, truncated, phase_count
):
    scenario.stdout = stdout
    scenario.output_truncated = truncated
    scenario.phase_count = phase_count
    await verify(scenario)
    assert not scenario.result.hidden_check
    assert not scenario.result.green


@pytest.mark.asyncio
@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
async def test_ci_requires_at_least_one_successfully_executed_check(
    scenario, conclusion
):
    scenario.conclusion = conclusion
    await verify(scenario)
    assert scenario.builds == 0
    assert not scenario.result.green


@pytest.mark.asyncio
async def test_ci_rerun_failure_after_independent_build_cannot_pass(scenario):
    scenario.final_conclusion = "failure"
    await verify(scenario)
    assert scenario.result.ci == "failure"
    assert not scenario.result.green
    assert not scenario.result.hidden_check


@pytest.mark.asyncio
async def test_commit_without_pinned_baseline_ancestry_is_rejected(scenario):
    git(scenario.repository, "checkout", "--orphan", "unrelated")
    git(scenario.repository, "add", ".")
    git(scenario.repository, "commit", "-m", "unrelated history")
    head = git(scenario.repository, "rev-parse", "HEAD")
    git(scenario.repository, "branch", "-f", "forgehand/wf", head)
    scenario.result.commit_sha = scenario.pull["head"]["sha"] = head
    with pytest.raises(ValueError, match="verification_base_not_ancestor"):
        await verify(scenario)
    assert not scenario.result.green
    assert scenario.builds == 0
    assert all(not manager._root.exists() for manager in scenario.managers)


@pytest.mark.asyncio
async def test_empty_published_diff_cannot_qualify(scenario):
    git(scenario.repository, "checkout", "main")
    git(scenario.repository, "branch", "-f", "forgehand/wf", scenario.case.base_sha)
    scenario.result.commit_sha = scenario.pull["head"]["sha"] = scenario.case.base_sha
    await verify(scenario)
    assert scenario.result.changed_paths == []
    assert not scenario.result.green
    assert scenario.builds == 0


@pytest.mark.asyncio
async def test_failed_recheck_does_not_reuse_old_verifier_success(scenario):
    await verify(scenario)
    assert scenario.result.green
    scenario.final_conclusion = "failure"
    await verify(scenario)
    assert not scenario.result.hidden_check
    assert scenario.result.verified_commit_sha is None
    assert not scenario.result.green
