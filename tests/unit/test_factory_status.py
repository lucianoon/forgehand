from app.api.routes.workflows import _to_response
from app.graph.state import WorkflowPhase


def status_data() -> dict[str, object]:
    return {
        "workflow_id": "wf-strategy",
        "phase": WorkflowPhase.LOADING_CONTEXT,
        "iteration": 0,
        "usage": {},
        "tasks": [],
        "pending_decision": None,
        "final_output": None,
        "error": None,
        "delivery": None,
        "work_order": None,
        "factory_stage": "strategy_selection",
        "phase_evidence": {
            "profile_name": "python-tests",
            "profile_digest": "a" * 64,
            "outcome": "success",
            "phases": [],
            "error_code": None,
        },
        "build_strategy": {
            "requested_profile": "python-tests",
            "selected_profile": "python-tests",
            "selection_reason": "explicit",
            "phases": ["test"],
            "profile_digest": "a" * 64,
            "unsupported_reason": None,
        },
    }


def test_factory_status_exposes_selected_profile_and_reason() -> None:
    response = _to_response(status_data())  # type: ignore[arg-type]

    assert response.factory_stage == "strategy_selection"
    assert response.build_strategy is not None
    assert response.build_strategy["selected_profile"] == "python-tests"
    assert response.build_strategy["selection_reason"] == "explicit"
    assert response.phase_evidence is not None
    assert response.phase_evidence["outcome"] == "success"


def test_unsupported_strategy_remains_visible_during_human_interrupt() -> None:
    data = status_data()
    data["phase"] = WorkflowPhase.UNSUPPORTED_BUILD_STRATEGY
    data["pending_decision"] = {
        "reason": "unsupported_build_strategy",
        "options": ["retry", "abort"],
    }
    strategy = dict(data["build_strategy"])  # type: ignore[arg-type]
    strategy.update(
        selected_profile=None,
        selection_reason="unsupported",
        profile_digest=None,
        unsupported_reason="perfil desconhecido",
    )
    data["build_strategy"] = strategy

    response = _to_response(data)  # type: ignore[arg-type]

    assert response.status == "awaiting_decision"
    assert response.current_stage == "unsupported_build_strategy"
    assert response.pending_decision is not None
    assert response.pending_decision["options"] == ["retry", "abort"]


def test_ready_for_review_is_terminal_and_explains_next_human_action():
    data = status_data()
    data["phase"] = WorkflowPhase.READY_FOR_HUMAN_REVIEW
    data["factory_stage"] = "ready_for_human_review"
    response = _to_response(data)
    assert response.status == "ready_for_human_review"
    assert response.current_stage == "ready_for_human_review"
    assert response.next_human_action is not None
    assert "merge no GitHub" in response.next_human_action
