from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.factory.delivery import factory_delivery_config, factory_ready_for_review
from app.graph.state import DeliveryConfig, DeliveryResult, WorkflowState
from app.models.build_execution import BuildOutcome, BuildPhaseResult, BuildRunResult
from app.models.factory import (
    BuildProfileSelection,
    DirectWorkOrderSource,
    RepositoryTarget,
    WorkOrder,
    WorkspaceLease,
    WorkspaceLifecycle,
)
from app.models.task import AgentTask, Capability, TaskAttempt, TaskStatus


def approved_state() -> WorkflowState:
    report = BuildRunResult(
        profile_name="python",
        profile_digest="d" * 64,
        outcome=BuildOutcome.SUCCESS,
        phases=(
            BuildPhaseResult(
                phase="test",
                outcome=BuildOutcome.SUCCESS,
                command=("/usr/bin/python",),
                image="python@sha256:" + "e" * 64,
                cwd=".",
                duration_seconds=0.1,
                exit_code=0,
            ),
        ),
    )
    target = RepositoryTarget(full_name="acme/widget")
    return WorkflowState(
        request="corrigir o widget",
        project_id="p",
        workflow_id="wf",
        owner_client_id="c",
        work_order=WorkOrder(
            source=DirectWorkOrderSource(),
            repository=target,
            requested_outcome="corrigir o widget",
            acceptance_criteria=["ok"],
        ),
        workspace=WorkspaceLease(
            workflow_id="wf",
            repository=target,
            local_path="/tmp/lease",
            branch="forgehand/wf",
            base_sha="b" * 40,
            state=WorkspaceLifecycle.READY,
        ),
        build_strategy=BuildProfileSelection(
            selected_profile="python",
            selection_reason="explicit",
            profile_digest="d" * 64,
            phases=["test"],
        ),
        plan=[
            AgentTask(
                title="fix",
                description="fix",
                capability=Capability.BACKEND,
                acceptance_criteria=["ok"],
                status=TaskStatus.COMPLETED,
                attempts=[
                    TaskAttempt(
                        attempt_number=1,
                        agent_name="fake",
                        model="fake",
                        started_at=datetime.now(timezone.utc),
                        build_validation=report,
                    )
                ],
            )
        ],
    )


def green_result() -> DeliveryResult:
    return DeliveryResult(
        pull_request_number=1,
        url="https://github.com/acme/widget/pull/1",
        branch="forgehand/wf",
        commit_sha="c" * 40,
        ci_state="success",
    )


def test_delivery_target_is_derived_from_lease_and_order():
    state = approved_state()
    config = factory_delivery_config(state)
    assert config.repository == "acme/widget"
    assert config.head_branch == "forgehand/wf"
    assert config.pinned_base_sha == "b" * 40
    assert config.expected_head_sha is None
    state.delivery_result = green_result()
    assert factory_delivery_config(state).expected_head_sha == "c" * 40
    assert factory_ready_for_review(state)


@pytest.mark.parametrize(
    "evidence",
    ["missing", "wrong_policy", "incomplete", "empty", "violation", "passed"],
)
def test_architecture_evidence_is_required_even_with_green_build(evidence):
    from app.models.architecture import ArchitectureFinding, ArchitectureReport

    state = approved_state()
    state.build_strategy = state.build_strategy.model_copy(
        update={"architecture_digest": "a" * 64}
    )
    architecture = (
        None
        if evidence == "missing"
        else ArchitectureReport(
            policy_digest=("b" if evidence == "wrong_policy" else "a") * 64,
            complete=evidence != "incomplete",
            files_checked=0 if evidence == "empty" else 1,
            findings=(
                ArchitectureFinding(
                    rule_id="domain",
                    code="forbidden_dependency",
                    path="domain.py",
                    line=1,
                    dependency="requests",
                    message="Forbidden dependency",
                    remediation="Use a domain interface.",
                ),
            )
            if evidence == "violation"
            else (),
        )
    )
    attempt = state.plan[0].attempts[0]
    attempt.build_validation = attempt.build_validation.model_copy(
        update={"architecture": architecture}
    )
    if evidence == "passed":
        assert factory_delivery_config(state).repository == "acme/widget"
    else:
        with pytest.raises(ValueError, match="validation_missing_or_failed"):
            factory_delivery_config(state)


@pytest.mark.parametrize(
    "update",
    [
        {"profile_digest": "f" * 64},
        {"profile_name": "other"},
        {"phases": ()},
        {"outcome": BuildOutcome.TIMEOUT},
        {"error_code": "cleanup_failed"},
    ],
)
def test_incomplete_or_wrong_evidence_blocks_delivery(update):
    state = approved_state()
    attempt = state.plan[0].attempts[-1]
    attempt.build_validation = attempt.build_validation.model_copy(update=update)
    with pytest.raises(ValueError, match="validation_missing_or_failed"):
        factory_delivery_config(state)


@pytest.mark.parametrize(
    "update",
    [
        {"cleanup_failed": True},
        {"exit_code": 1},
        {"outcome": BuildOutcome.INFRASTRUCTURE_ERROR},
    ],
)
def test_top_level_success_cannot_hide_failed_phase(update):
    state = approved_state()
    attempt = state.plan[0].attempts[-1]
    report = attempt.build_validation
    attempt.build_validation = report.model_copy(
        update={"phases": (report.phases[0].model_copy(update=update),)}
    )
    with pytest.raises(ValueError, match="validation_missing_or_failed"):
        factory_delivery_config(state)


@pytest.mark.parametrize(
    "config",
    [
        DeliveryConfig(repository="other/repo"),
        DeliveryConfig(repository="acme/widget", head_branch="main"),
        DeliveryConfig(repository="acme/widget", pinned_base_sha="f" * 40),
    ],
)
def test_destination_override_is_rejected(config):
    state = approved_state()
    state.delivery = config
    with pytest.raises(ValueError, match="destination_mismatch"):
        factory_delivery_config(state)


@pytest.mark.parametrize("status", ["pending", "none", "skipped", "error", "failure"])
def test_unconfirmed_ci_is_never_ready(status):
    state = approved_state()
    state.delivery_result = green_result().model_copy(update={"ci_state": status})
    assert not factory_ready_for_review(state)


def test_missing_evidence_or_partial_acceptance_cannot_publish():
    state = approved_state()
    state.plan[0].attempts = []
    with pytest.raises(ValueError, match="validation_missing_or_failed"):
        factory_delivery_config(state)
    state = approved_state()
    state.human_decision = "accept_partial"
    with pytest.raises(ValueError, match="requires_all_tasks_approved"):
        factory_delivery_config(state)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "paths,expected", [(["b.py"], 1), (["unknown.py"], 0), ([], 0)]
)
async def test_ci_only_reopens_responsible_task(paths, expected):
    from app.graph.nodes import build_nodes

    state = approved_state()
    first = state.plan[0]
    first.result = {
        "workspace": {"published_files": [{"path": "a.py", "content": "a"}]}
    }
    second = first.model_copy(
        deep=True,
        update={
            "id": uuid4(),
            "result": {
                "workspace": {"published_files": [{"path": "b.py", "content": "b"}]}
            },
        },
    )
    state.plan = [first, second]

    class Publisher:
        async def publish(self, **kwargs):
            return green_result().model_copy(
                update={
                    "ci_state": "failure",
                    "failure_paths": paths,
                    "failures": ["unit test failed"],
                }
            )

    nodes = build_nodes(None, None, None, None, delivery=Publisher())
    result = await nodes["publish_delivery"](state)
    assert len(result["plan"]) == expected
    if expected:
        assert result["plan"][0].id == second.id
        assert result["plan"][0].status == TaskStatus.REJECTED
    else:
        state.delivery_result = result["delivery_result"]
        assert nodes["delivery_result_router"](state) == "human_gate"
