"""Sandbox phase evidence reaches typed criteria before subjective review."""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from app.agents.judge import LLMJudge, SYSTEM_PROMPT as JUDGE_PROMPT
from app.agents.planner import SYSTEM_PROMPT as PLANNER_PROMPT
from app.agents.validation import ValidationSignal
from app.api.container import LeaseBoundRuntimeFactory
from app.infrastructure.settings import Settings
from app.models.build_execution import BuildOutcome, BuildPhaseResult, BuildRunResult
from app.models.factory import RepositoryTarget, WorkspaceLease
from app.models.task import AgentTask, Capability, TaskAttempt
from app.providers.registry import ProviderRouter


class NoLLM:
    async def complete(self, tier, request):
        raise AssertionError("objective sandbox criteria must not reach the LLM")


def phase(name="test", **updates):
    return BuildPhaseResult(
        phase=name,
        outcome=BuildOutcome.SUCCESS,
        command=("/usr/local/bin/python", "-m", "pytest"),
        image="python@sha256:" + "a" * 64,
        cwd=".",
        duration_seconds=0.1,
        exit_code=0,
    ).model_copy(update=updates)


def report(*phases):
    return BuildRunResult(
        profile_name="python", profile_digest="d" * 64,
        outcome=BuildOutcome.SUCCESS, phases=phases or (phase(),),
    )


def attempt(build_report, number=1):
    return TaskAttempt(
        attempt_number=number, agent_name="executor", model="fake",
        started_at=datetime.now(timezone.utc), build_validation=build_report,
    )


def task(build_report, criteria=None):
    return AgentTask(
        title="Corrigir total", description="Aplicar desconto uma única vez",
        capability=Capability.BACKEND,
        acceptance_criteria=criteria or [{"text": "Testes passam", "kind": "tests_pass"}],
        attempts=[attempt(build_report)], result={"summary": "Implementação corrigida"},
    )


@pytest.mark.asyncio
async def test_live_case_uses_sandbox_test_evidence_without_subjective_rejudgment():
    # The failed live plan put this behavioral label on a tests_pass criterion.
    # Preserve the actual kind's contract: running tests is objective, whereas
    # test coverage remains a separate task's obligation (planner guidance below).
    current = task(report(), [{
        "text": "Lista vazia de preços resulta em 0", "kind": "tests_pass",
        "path": "tests/test_orders.py",
    }])
    outcome = await LLMJudge(NoLLM(), require_build_evidence=True).evaluate(current, {})
    assert outcome.evaluation.approved
    assert outcome.evaluation.tests_passed is True
    assert outcome.evaluation.criteria_scores == {"Lista vazia de preços resulta em 0": 1.0}
    assert outcome.evaluation.required_changes == []
    assert "criteria" in outcome.evaluation.validated_by
    assert "llm" not in outcome.evaluation.validated_by
    assert outcome.usage.tokens == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("name,kind,field", [
    ("test", "tests_pass", "tests_passed"),
    ("lint", "lint_pass", "lint_passed"),
    ("types", "types_pass", "type_check_passed"),
])
async def test_each_phase_proves_only_its_own_signal(name, kind, field):
    criteria = [{"text": kind, "kind": kind}]
    evaluation = (await LLMJudge(NoLLM(), require_build_evidence=True).evaluate(
        task(report(phase(name)), criteria), {}
    )).evaluation
    assert evaluation.approved and getattr(evaluation, field) is True
    for other in {"tests_passed", "lint_passed", "type_check_passed"} - {field}:
        assert getattr(evaluation, other) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("present", ["prepare", "build", "lint", "types"])
async def test_other_successful_phases_do_not_prove_tests_ran(present):
    evaluation = (await LLMJudge(NoLLM(), require_build_evidence=True).evaluate(
        task(report(phase(present))), {}
    )).evaluation
    assert not evaluation.approved
    assert evaluation.tests_passed is False
    assert evaluation.criteria_scores == {"Testes passam": 0.0}
    assert any("ausente na tentativa atual" in failure for failure in evaluation.failures)


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"outcome": BuildOutcome.COMMAND_FAILURE, "exit_code": 1},
    {"outcome": BuildOutcome.TIMEOUT},
    {"outcome": BuildOutcome.CANCELLED},
    {"cleanup_failed": True},
    {"exit_code": None},
    {"error_code": "sandbox_cleanup_failed"},
])
async def test_failed_incomplete_or_unclean_test_phase_cannot_be_green(updates):
    # Top-level success cannot mask failed/incomplete details.
    evaluation = (await LLMJudge(NoLLM(), require_build_evidence=True).evaluate(
        task(report(phase(**updates))), {}
    )).evaluation
    assert not evaluation.approved
    assert evaluation.tests_passed is False


@pytest.mark.asyncio
async def test_stale_attempt_and_executor_workspace_reports_cannot_replace_current_evidence():
    current = task(report())
    current.attempts.append(attempt(None, number=2))
    current.result["workspace"] = {
        "build_validation": report().model_dump(mode="json"),
        "command_feedback": [{"name": "pytest", "passed": True}],
    }
    evaluation = (await LLMJudge(NoLLM(), require_build_evidence=True).evaluate(current, {})).evaluation
    assert not evaluation.approved
    assert evaluation.tests_passed is False
    assert any("relatório sandbox ausente" in failure for failure in evaluation.failures)


@pytest.mark.asyncio
async def test_successful_tests_do_not_hide_failed_other_phase():
    evaluation = (await LLMJudge(NoLLM(), require_build_evidence=True).evaluate(
        task(report(phase(), phase("build", outcome=BuildOutcome.COMMAND_FAILURE, exit_code=1))), {}
    )).evaluation
    assert not evaluation.approved
    assert evaluation.tests_passed is True
    assert evaluation.criteria_scores == {"Testes passam": 1.0}
    assert any(failure.startswith("[sandbox]") for failure in evaluation.failures)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_passed,sandbox_passed", [(False, True), (True, False)])
async def test_successful_validator_never_erases_failure_with_same_signal(legacy_passed, sandbox_passed):
    class Validator:
        name = "pytest"

        async def validate(self, current):
            return ValidationSignal(name="pytest", passed=legacy_passed, details="legacy")

    test_phase = phase() if sandbox_passed else phase(exit_code=1, outcome=BuildOutcome.COMMAND_FAILURE)
    evaluation = (await LLMJudge(NoLLM(), validators=[Validator()], require_build_evidence=True).evaluate(
        task(report(test_phase)), {}
    )).evaluation
    assert not evaluation.approved
    assert evaluation.tests_passed is False
    assert evaluation.criteria_scores == {"Testes passam": 0.0}


@pytest.mark.asyncio
async def test_factory_wiring_requires_current_build_evidence(tmp_path):
    router = Mock(spec=ProviderRouter)
    lease = WorkspaceLease(
        workflow_id="judge", repository=RepositoryTarget(full_name="fixture/test"),
        local_path=str(tmp_path), branch="forgehand/judge", base_sha="a" * 40,
    )
    judge = LeaseBoundRuntimeFactory(Settings(), router).build_judge(lease)
    evaluation = (await judge.evaluate(task(None), {})).evaluation
    assert not evaluation.approved
    assert evaluation.tests_passed is False
    router.complete.assert_not_called()


def test_planner_and_judge_keep_behavior_coverage_and_downstream_obligations_separate():
    assert "não provam cobertura de um caso específico" in PLANNER_PROMPT
    assert "cada tarefa deve ser aprovável antes das tarefas que dependem dela" in PLANNER_PROMPT
    assert "sem exigir o trabalho futuro na antecessora" in PLANNER_PROMPT
    assert "Exija testes novos quando o contrato desta tarefa pedir cobertura ou regressão" in JUDGE_PROMPT
    assert "tarefas dependentes, validação integrada e gates finais continuam obrigatórios" in JUDGE_PROMPT
