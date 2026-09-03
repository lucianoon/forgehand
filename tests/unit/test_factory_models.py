from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.graph.state import WorkflowState
from app.models.factory import (
    BuildProfileSelection,
    DirectWorkOrderSource,
    FactoryStage,
    GitHubIssueSnapshot,
    GitHubIssueWorkOrderSource,
    RepositoryTarget,
    WorkOrder,
    WorkspaceLease,
    WorkspaceLifecycle,
)
from app.models.task import TaskAttempt


def direct_order() -> WorkOrder:
    return WorkOrder(
        source=DirectWorkOrderSource(),
        repository=RepositoryTarget(full_name="acme/widgets"),
        requested_outcome="Corrigir o cálculo do total.",
        acceptance_criteria=["Todos os testes passam"],
    )


def test_work_order_round_trips_as_json() -> None:
    order = direct_order()

    restored = WorkOrder.model_validate_json(order.model_dump_json())

    assert restored == order
    assert restored.source.kind == "direct"
    assert restored.delivery_policy.require_human_merge is True


def test_issue_snapshot_must_match_target_repository() -> None:
    snapshot = GitHubIssueSnapshot(
        url="https://github.com/acme/other/issues/7",
        number=7,
        title="Bug",
        repository="acme/other",
        author="octocat",
        updated_at=datetime.now(timezone.utc),
    )

    with pytest.raises(ValidationError, match="mesmo repositório"):
        WorkOrder(
            source=GitHubIssueWorkOrderSource(snapshot=snapshot),
            repository=RepositoryTarget(full_name="acme/widgets"),
            requested_outcome="Corrigir o comportamento descrito.",
            acceptance_criteria=["Teste de regressão passa"],
        )


def test_workspace_lease_requires_absolute_path_and_serializes() -> None:
    lease = WorkspaceLease(
        workflow_id="wf-1",
        repository=RepositoryTarget(full_name="acme/widgets"),
        local_path="/var/lib/forgehand/wf-1",
        branch="forgehand/wf-1",
        base_sha="a" * 40,
        state=WorkspaceLifecycle.READY,
    )

    assert WorkspaceLease.model_validate_json(lease.model_dump_json()) == lease

    with pytest.raises(ValidationError, match="absoluto"):
        lease.model_copy(update={"local_path": "relative"}).model_dump()
        WorkspaceLease.model_validate(
            {**lease.model_dump(), "local_path": "relative"}
        )


def test_legacy_workflow_state_gets_factory_defaults() -> None:
    legacy = {
        "request": "Executar uma análise legada",
        "project_id": "legacy",
        "workflow_id": "wf-legacy",
        "owner_client_id": "client",
    }

    state = WorkflowState.model_validate(legacy)

    assert state.work_order is None
    assert state.workspace is None
    assert state.build_strategy is None
    assert state.factory_stage is None


def test_factory_state_and_attempt_round_trip() -> None:
    order = direct_order()
    strategy = BuildProfileSelection(
        requested_profile="python",
        selected_profile="python",
        selection_reason="explicit",
        phases=["lint", "test"],
    )
    lease = WorkspaceLease(
        workflow_id="wf-factory",
        repository=order.repository,
        local_path="/var/lib/forgehand/wf-factory",
        branch="forgehand/wf-factory",
        base_sha="b" * 40,
        state=WorkspaceLifecycle.ACTIVE,
    )
    state = WorkflowState(
        request=order.requested_outcome,
        project_id="factory",
        workflow_id="wf-factory",
        owner_client_id="client",
        work_order=order,
        workspace=lease,
        build_strategy=strategy,
        factory_stage=FactoryStage.IMPLEMENTATION,
    )
    attempt = TaskAttempt(
        attempt_number=1,
        agent_name="backend",
        model="test-model",
        started_at=datetime.now(timezone.utc),
        factory_stage=FactoryStage.VALIDATION,
        build_strategy=strategy,
    )

    restored = WorkflowState.model_validate_json(state.model_dump_json())
    restored_attempt = TaskAttempt.model_validate_json(attempt.model_dump_json())

    assert restored.work_order == order
    assert restored.workspace == lease
    assert restored_attempt.build_strategy == strategy
    assert restored_attempt.factory_stage is FactoryStage.VALIDATION
