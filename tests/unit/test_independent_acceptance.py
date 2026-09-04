import os
import asyncio
import hashlib
import json

import pytest

from app.factory.acceptance import acceptance_verified
from app.factory.build_strategy import BuildProfileRegistry
from app.factory.delivery import factory_delivery_config
from app.factory.sandbox import BuildRunCancelled, DockerBuildRunner, DockerOutput
from app.models.build import AcceptanceSuite
from app.models.build_execution import BuildOutcome, BuildRunResult
from app.models.factory import BuildProfileSelection, DirectWorkOrderSource, WorkOrder
from tests.unit.test_factory_sandbox import FakeDocker, make_lease, make_profile
from tests.unit.test_factory_delivery import approved_state

# Factory mode é POSIX por design (lock fcntl, dir_fd/O_NOFOLLOW, grupo de
# processos, caminhos de lease em /): no Windows só o mission control roda.
pytestmark = pytest.mark.skipif(os.name != "posix", reason="factory mode exige POSIX")

CRITERION = "Somar inteiros corretamente"


def test_documented_operator_profile_validates():
    from pathlib import Path
    from app.infrastructure.settings import Settings

    document = (
        Path(__file__).resolve().parents[2] / "docs/independent-acceptance.md"
    ).read_text()
    raw = document.split("```json\n", 1)[1].split("```", 1)[0]
    profiles = Settings(
        _env_file=None, factory_build_profiles_json=raw
    ).factory_build_profiles
    assert len(profiles["cli-integers"].acceptance.cases) == 2


def suite(**overrides):
    return AcceptanceSuite.model_validate(
        {
            "cases": [
                {
                    "id": "sum",
                    "criterion": CRITERION,
                    "command": {
                        "name": "test",
                        "argv": ["/usr/local/bin/python", "calc.py", "2", "3"],
                        "timeout_seconds": 5,
                    },
                    "expected_stdout": "5\n",
                    **overrides,
                }
            ]
        }
    )


def selected(profile, root, criteria=None):
    lease = make_lease(root)
    order = WorkOrder(
        source=DirectWorkOrderSource(),
        repository=lease.repository,
        requested_outcome="Implementar soma de inteiros pela CLI",
        acceptance_criteria=criteria or [CRITERION],
        build_profile=BuildProfileSelection(requested_profile=profile.name),
    )
    registry = BuildProfileRegistry({profile.name: profile})
    return registry, registry.select(order, lease)


@pytest.mark.parametrize(
    "change",
    [
        {
            "command": {
                "name": "prepare",
                "argv": ["/usr/local/bin/python", "calc.py"],
                "network": "dependencies",
                "timeout_seconds": 5,
            }
        },
        {
            "command": {
                "name": "test",
                "argv": ["/usr/local/bin/python", "../escape.py"],
                "timeout_seconds": 5,
            }
        },
        {
            "command": {
                "name": "test",
                "argv": ["/bin/sh", "calc.py"],
                "timeout_seconds": 5,
            }
        },
        {
            "command": {
                "name": "test",
                "argv": ["/usr/local/bin/python", "calc.py"],
                "timeout_seconds": 31,
            }
        },
        {
            "command": {
                "name": "test",
                "argv": ["/usr/local/bin/python", "calc.py"],
                "timeout_seconds": 5,
                "output_limit": 20_000,
            }
        },
        {
            "command": {
                "name": "test",
                "argv": ["/usr/local/bin/python", "calc.py"],
                "timeout_seconds": 5,
                "environment": {"OPENAI_API_KEY": "forbidden"},
            }
        },
        {"expected_stdout": "a" * 8193},
        {"expected_stdout": "€" * 3000},
        {"id": "../test"},
        {"criterion": ""},
        {"unapproved": True},
    ],
)
def test_invalid_acceptance_contracts_rejected(change):
    with pytest.raises(ValueError):
        suite(**change)


def test_suite_bounds():
    case = suite().model_dump()["cases"][0]
    for cases in [[], [case, case], [{**case, "id": f"case-{i}"} for i in range(9)]]:
        with pytest.raises(ValueError):
            AcceptanceSuite(cases=cases)
    with pytest.raises(ValueError):
        AcceptanceSuite(
            cases=[
                {
                    **case,
                    "id": f"case-{i}",
                    "command": {**case["command"], "timeout_seconds": 30},
                }
                for i in range(5)
            ]
        )


@pytest.mark.asyncio
async def test_complete_case_set_and_checkpoint_roundtrip(tmp_path):
    from app.graph.workflow import build_serde

    one = suite().cases[0]
    policy = AcceptanceSuite(
        cases=(one, one.model_copy(update={"id": "sum-regression"}))
    )
    profile = make_profile().model_copy(update={"acceptance": policy})
    registry, selection = selected(profile, tmp_path)
    report = await DockerBuildRunner(registry, AcceptanceDocker()).run(
        make_lease(tmp_path), selection
    )
    assert report.acceptance.passed
    assert len(report.acceptance.cases) == 2
    serde = build_serde()
    restored = serde.loads_typed(serde.dumps_typed(report))
    assert restored == report
    assert acceptance_verified(restored.acceptance, selection)
    partial = report.acceptance.model_copy(
        update={"cases": report.acceptance.cases[:1]}
    )
    assert partial.passed
    assert not acceptance_verified(partial, selection)
    case = report.acceptance.cases[0].model_copy(update={"case_digest": "f" * 64})
    forged = report.acceptance.model_copy(
        update={"cases": (case, report.acceptance.cases[1])}
    )
    assert not acceptance_verified(forged, selection)


def test_pinned_coverage_and_legacy_fingerprint(tmp_path):
    base = make_profile()
    legacy = base.model_dump(mode="json")
    legacy.pop("acceptance")
    legacy.pop("architecture")
    assert (
        base.fingerprint()
        == hashlib.sha256(
            json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    profile = base.model_copy(update={"acceptance": suite()})
    registry, selection = selected(profile, tmp_path)
    assert registry.profile_for(selection) == profile
    assert selection.acceptance_criteria == [CRITERION]
    assert selection.acceptance_cases == {"sum": suite().cases[0].fingerprint()}
    _, uncovered = selected(
        profile, tmp_path, [CRITERION, "Preservar registros existentes"]
    )
    assert uncovered.selection_reason == "unsupported"
    for change in [
        {"acceptance_cases": {}},
        {"acceptance_digest": None},
        {"acceptance_criteria": []},
    ]:
        with pytest.raises(ValueError):
            registry.profile_for(selection.model_copy(update=change))
    changed = profile.model_copy(update={"acceptance": suite(expected_stdout="6\n")})
    with pytest.raises(ValueError):
        BuildProfileRegistry({profile.name: changed}).profile_for(selection)


class AcceptanceDocker(FakeDocker):
    def __init__(self, output=None, *, cleanup_fails=False, cancel=False):
        super().__init__()
        self.output = output or DockerOutput(0, "5\n")
        self.creates = 0
        self.cleanup_fails = cleanup_fails
        self.cancel = cancel

    async def call(self, args, **kwargs):
        if args[0] == "create":
            self.creates += 1
        if self.creates > 1 and args[0] == "start":
            self.calls.append((args, kwargs["timeout"], kwargs["output_limit"]))
            if self.cancel:
                raise asyncio.CancelledError()
            self.exit_code = self.output.exit_code or 0
            return self.output
        if self.creates > 1 and args[0] == "rm" and self.cleanup_fails:
            return DockerOutput(1, stderr="injected cleanup failure")
        return await super().call(args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output,passed",
    [
        (DockerOutput(0, "5\n"), True),
        (DockerOutput(0, "tests passed\n"), False),
        (DockerOutput(0, "5"), False),
        (DockerOutput(0, "\x1b[31m5\n"), False),
        (DockerOutput(0, "5\n", truncated=True), False),
        (DockerOutput(0, "5\n", timed_out=True), False),
        (DockerOutput(1, "5\n"), False),
    ],
)
async def test_host_oracle_rejects_false_green_and_incomplete_output(
    tmp_path, output, passed
):
    profile = make_profile().model_copy(update={"acceptance": suite()})
    registry, selection = selected(profile, tmp_path)
    docker = AcceptanceDocker(output)
    report = await DockerBuildRunner(registry, docker).run(
        make_lease(tmp_path), selection
    )
    assert report.phases[0].outcome == BuildOutcome.SUCCESS
    assert report.acceptance.passed is passed
    assert acceptance_verified(report.acceptance, selection) is passed
    assert (report.outcome == BuildOutcome.SUCCESS) is passed
    assert BuildRunResult.model_validate(report.model_dump()) == report
    creates = [args for args, _, _ in docker.calls if args[0] == "create"]
    assert ",readonly" not in creates[0][creates[0].index("--mount") + 1]
    assert creates[1][creates[1].index("--mount") + 1].endswith(",readonly")
    assert creates[1][creates[1].index("--network") + 1] == "none"
    assert "5\n" not in creates[1]
    if output.stdout.startswith("\x1b"):
        assert report.acceptance.cases[0].execution.stdout == "5\n"
        assert not report.acceptance.cases[
            0
        ].passed  # Sanitization cannot create success.


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_acceptance_cleanup_and_cancellation_are_fail_closed(tmp_path, cancel):
    profile = make_profile().model_copy(update={"acceptance": suite()})
    registry, selection = selected(profile, tmp_path)
    docker = AcceptanceDocker(cleanup_fails=not cancel, cancel=cancel)
    runner = DockerBuildRunner(registry, docker)
    if cancel:
        with pytest.raises(BuildRunCancelled) as exc:
            await runner.run(make_lease(tmp_path), selection)
        report = exc.value.report
        assert not runner.active_containers
    else:
        report = await runner.run(make_lease(tmp_path), selection)
        assert runner.active_containers
    assert not report.acceptance.passed
    assert report.outcome != BuildOutcome.SUCCESS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "digest",
        "cases",
        "duplicate",
        "criterion",
        "incomplete",
        "unsafe_mount",
        "wrong_output",
    ],
)
async def test_publication_rejects_forged_aggregate_green(tmp_path, mutation):
    profile = make_profile().model_copy(update={"acceptance": suite()})
    registry, selection = selected(profile, tmp_path)
    report = await DockerBuildRunner(registry, AcceptanceDocker()).run(
        make_lease(tmp_path), selection
    )
    state = approved_state()
    state.work_order = state.work_order.model_copy(
        update={"acceptance_criteria": [CRITERION]}
    )
    state.build_strategy = selection
    state.plan[0].attempts[0] = (
        state.plan[0].attempts[0].model_copy(update={"build_validation": report})
    )
    assert factory_delivery_config(state)
    evidence = report.acceptance
    if mutation == "missing":
        evidence = None
    elif mutation == "digest":
        evidence = evidence.model_copy(update={"suite_digest": "a" * 64})
    elif mutation == "cases":
        evidence = evidence.model_copy(update={"cases": ()})
    elif mutation == "duplicate":
        evidence = evidence.model_copy(update={"cases": evidence.cases * 2})
    elif mutation == "criterion":
        evidence = evidence.model_copy(update={"required_criteria": ("Other",)})
    elif mutation == "incomplete":
        evidence = evidence.model_copy(update={"complete": False})
    else:
        execution = evidence.cases[0].execution.model_copy(
            update=(
                {"workspace_read_only": False}
                if mutation == "unsafe_mount"
                else {"stdout_sha256": "b" * 64}
            )
        )
        evidence = evidence.model_copy(
            update={
                "cases": (
                    evidence.cases[0].model_copy(update={"execution": execution}),
                )
            }
        )
    changed = report.model_copy(
        update={"acceptance": evidence, "outcome": BuildOutcome.SUCCESS}
    )
    state.plan[0].attempts[0] = (
        state.plan[0].attempts[0].model_copy(update={"build_validation": changed})
    )
    with pytest.raises(ValueError):
        factory_delivery_config(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", [False, True])
async def test_graph_veto_and_feedback_override_model_approval(tmp_path, repair):
    from tests.unit.test_factory_graph import (
        BuildRunner,
        Delivery,
        FactoryRuntime,
        RecordingMemory,
        WorkspaceManager,
        graph,
        lease,
        work_order,
    )
    from app.agents.executor import LLMExecutor

    policy = suite()
    profile = make_profile().model_copy(update={"acceptance": policy})
    registry, selection = selected(profile, tmp_path)
    good = await DockerBuildRunner(registry, AcceptanceDocker()).run(
        make_lease(tmp_path), selection
    )
    bad = good.model_copy(update={"acceptance": None})
    runtime, publisher = FactoryRuntime(tmp_path), Delivery()
    runner = BuildRunner([bad, good] if repair else [bad])
    result = await graph(
        WorkspaceManager(lease(tmp_path, "acceptance-graph")),
        runtime,
        RecordingMemory(),
        build_runner=runner,
        delivery=publisher,
        acceptance_suite=policy,
    ).ainvoke(
        {
            "request": "Implementar soma correta",
            "project_id": "p",
            "workflow_id": "acceptance-graph",
            "owner_client_id": "c",
            "work_order": work_order().model_copy(
                update={"acceptance_criteria": [CRITERION]}
            ),
        },
        {"configurable": {"thread_id": "acceptance-graph"}},
    )
    assert not result["evaluations"][0].approved
    assert (
        result["plan"][0].attempts[0].build_validation.error_code
        == "acceptance_evidence_missing_or_failed"
    )
    assert bool(publisher.calls) is repair
    if repair:
        assert runner.calls == 2
        assert "Aceitação independente: aprovada" in result["final_output"]
    context = runtime.executor.contexts[0]
    prompt = LLMExecutor._build_user_content(
        None,
        runtime.executor.tasks[0],
        context,
        previous_feedback="",
        current_iteration_feedback="",
    )
    assert CRITERION in prompt
    assert "expected_stdout" not in json.dumps(context)
