"""Contrato de publicação da fábrica, derivado apenas do checkpoint confiável."""

from app.graph.state import DeliveryConfig, WorkflowState
from app.models.build_execution import BuildOutcome
from app.models.factory import WorkspaceLifecycle
from app.factory.acceptance import acceptance_verified


def factory_delivery_config(state: WorkflowState) -> DeliveryConfig:
    order, lease, selection = state.work_order, state.workspace, state.build_strategy
    if order is None or lease is None or selection is None:
        raise ValueError("factory_delivery_context_missing")
    if (
        lease.workflow_id != state.workflow_id
        or lease.repository != order.repository
        or lease.branch != f"forgehand/{state.workflow_id}"
        or lease.state not in {WorkspaceLifecycle.READY, WorkspaceLifecycle.ACTIVE}
        or order.repository.scm_host != "github.com"
    ):
        raise ValueError("factory_delivery_lease_mismatch")
    if not state.all_approved or state.human_decision == "accept_partial":
        raise ValueError("factory_delivery_requires_all_tasks_approved")
    if (
        not selection.selected_profile
        or not selection.profile_digest
        or not selection.phases
        or selection.selection_reason == "unsupported"
    ):
        raise ValueError("factory_delivery_strategy_missing")
    if selection.acceptance_digest is not None and selection.acceptance_criteria != order.acceptance_criteria:
        raise ValueError("factory_delivery_acceptance_criteria_mismatch")
    for task in state.plan:
        report = task.attempts[-1].build_validation if task.attempts else None
        if (
            report is None
            or report.profile_name != selection.selected_profile
            or report.profile_digest != selection.profile_digest
            or report.outcome != BuildOutcome.SUCCESS
            or report.error_code is not None
            or not acceptance_verified(report.acceptance, selection)
            or (report.architecture is not None and not report.architecture.passed)
            or (
                selection.architecture_digest is not None
                and (
                    report.architecture is None
                    or report.architecture.policy_digest
                    != selection.architecture_digest
                )
            )
            or [phase.phase.value for phase in report.phases] != selection.phases
            or any(
                phase.outcome != BuildOutcome.SUCCESS
                or phase.exit_code != 0
                or phase.cleanup_failed
                or phase.error_code is not None
                for phase in report.phases
            )
        ):
            raise ValueError("factory_delivery_validation_missing_or_failed")
    configured = state.delivery
    if configured is not None and (
        configured.repository != order.repository.full_name
        or configured.base_branch != order.repository.base_ref
        or configured.head_branch not in {None, lease.branch}
        or configured.pinned_base_sha not in {None, lease.base_sha.lower()}
    ):
        raise ValueError("factory_delivery_destination_mismatch")
    previous = state.delivery_result
    if previous is not None and previous.branch not in {None, lease.branch}:
        raise ValueError("factory_delivery_previous_branch_mismatch")
    return DeliveryConfig(
        repository=order.repository.full_name,
        base_branch=order.repository.base_ref,
        head_branch=lease.branch,
        pinned_base_sha=lease.base_sha.lower(),
        expected_head_sha=previous.commit_sha if previous else None,
        title=configured.title if configured else None,
        wait_for_checks=order.delivery_policy.wait_for_checks,
        checks_timeout_seconds=order.delivery_policy.checks_timeout_seconds,
    )


def factory_ready_for_review(state: WorkflowState) -> bool:
    try:
        config = factory_delivery_config(state)
    except ValueError:
        return False
    result = state.delivery_result
    return bool(
        result is not None
        and result.ci_state == "success"
        and result.pull_request_number
        and result.url
        and result.commit_sha
        and result.branch == config.head_branch
        and not result.error
        and not result.failures
    )
