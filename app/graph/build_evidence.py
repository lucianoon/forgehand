"""Evidência objetiva anexada às tarefas: sandbox, arquitetura e aceitação.

Funções puras sobre AgentTask/BuildRunResult, sem dependência injetada.
São usadas pela execução (veto do judge), pela revisão (síntese) e pela
entrega (resumo do PR).
"""

from __future__ import annotations

import json
from typing import Any

from app.agents.validation import format_validation_feedback
from app.factory.acceptance import acceptance_feedback
from app.factory.architecture import architecture_feedback
from app.graph.state import DeliveryResult
from app.models.build_execution import BuildOutcome, BuildRunResult
from app.models.task import AgentTask, EvaluationResult


def attempt_operational_summary(
    result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    workspace = result.get("workspace")
    if not isinstance(workspace, dict):
        return None
    command_feedback = workspace.get("command_feedback")
    file_diffs = workspace.get("file_diffs")
    operation_history = workspace.get("operation_history")
    git_snapshot = workspace.get("git_snapshot")
    strategy = workspace.get("strategy")
    autocorrect = workspace.get("autocorrect")
    validation_feedback = (
        command_feedback if isinstance(command_feedback, list) else []
    )
    return {
        "applied_files": workspace.get("applied_files", []),
        "diff_paths": [
            item.get("path")
            for item in file_diffs
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ]
        if isinstance(file_diffs, list)
        else [],
        "command_feedback": validation_feedback,
        "validation_feedback_text": format_validation_feedback(validation_feedback),
        "executed_commands": [
            item.get("command")
            for item in validation_feedback
            if isinstance(item, dict) and isinstance(item.get("command"), str)
        ]
        if validation_feedback
        else [],
        "operation_steps": len(operation_history)
        if isinstance(operation_history, list)
        else 0,
        "git_status": git_snapshot.get("status")
        if isinstance(git_snapshot, dict)
        else None,
        "strategy": strategy if isinstance(strategy, dict) else None,
        "autocorrect": autocorrect if isinstance(autocorrect, dict) else None,
        "build_validation": workspace.get("build_validation"),
    }


def build_report_from_task(task: AgentTask) -> BuildRunResult | None:
    if task.attempts and task.attempts[-1].build_validation is not None:
        return task.attempts[-1].build_validation
    if not isinstance(task.result, dict):
        return None
    workspace = task.result.get("workspace")
    if not isinstance(workspace, dict) or workspace.get("build_validation") is None:
        return None
    try:
        return BuildRunResult.model_validate(workspace["build_validation"])
    except ValueError:
        return None


def latest_build_report(tasks: list[AgentTask]) -> BuildRunResult | None:
    """Relatório da tarefa mais recente que tenha evidência de build."""
    return next(
        (
            candidate
            for task in reversed(tasks)
            if (candidate := build_report_from_task(task)) is not None
        ),
        None,
    )


def build_feedback(report: BuildRunResult) -> list[dict[str, Any]]:
    feedback = [
        {
            "name": f"sandbox:{phase.phase.value}",
            "passed": phase.outcome == BuildOutcome.SUCCESS,
            "command": json.dumps(list(phase.command), ensure_ascii=False),
            "exit_code": phase.exit_code,
            "details": phase.error_code or phase.outcome.value,
            "stdout": phase.stdout,
            "stderr": phase.stderr,
            "duration_seconds": phase.duration_seconds,
            "output_truncated": phase.output_truncated,
        }
        for phase in report.phases
    ]
    if not feedback and report.outcome != BuildOutcome.SUCCESS:
        feedback.append(
            {
                "name": "sandbox",
                "passed": False,
                "command": "",
                "exit_code": None,
                "details": report.error_code or report.outcome.value,
                "stdout": "",
                "stderr": "",
                "duration_seconds": 0.0,
                "output_truncated": False,
            }
        )
    if report.architecture is not None:
        feedback.extend(architecture_feedback(report.architecture))
    if report.acceptance is not None:
        feedback.extend(acceptance_feedback(report.acceptance))
    return feedback


def attach_build_report(
    result: dict[str, Any] | None, report: BuildRunResult
) -> dict[str, Any]:
    attached = dict(result) if isinstance(result, dict) else {}
    workspace = attached.get("workspace")
    workspace = dict(workspace) if isinstance(workspace, dict) else {}
    workspace["build_validation"] = report.model_dump(mode="json")
    existing = workspace.get("command_feedback")
    feedback = list(existing) if isinstance(existing, list) else []
    feedback.extend(build_feedback(report))
    workspace["command_feedback"] = feedback
    attached["workspace"] = workspace
    return attached


def apply_build_veto(
    evaluation: EvaluationResult, report: BuildRunResult | None
) -> EvaluationResult:
    """Fases obrigatórias reprovadas vetam a aprovação do judge, sempre."""
    if report is None:
        return evaluation
    validated_by = list(dict.fromkeys([*evaluation.validated_by, "sandbox"]))
    if report.architecture is not None:
        validated_by = list(dict.fromkeys([*validated_by, "architecture"]))
    if report.acceptance is not None:
        validated_by = list(dict.fromkeys([*validated_by, "independent_acceptance"]))
    phase_by_name = {phase.phase.value: phase for phase in report.phases}

    def signal(name: str, current: bool | None) -> bool | None:
        phase = phase_by_name.get(name)
        if phase is None:
            return current
        passed = phase.outcome == BuildOutcome.SUCCESS
        return passed if current is None else current and passed

    updates: dict[str, Any] = {
        "validated_by": validated_by,
        "tests_passed": signal("test", evaluation.tests_passed),
        "lint_passed": signal("lint", evaluation.lint_passed),
        "type_check_passed": signal("types", evaluation.type_check_passed),
    }
    if report.outcome != BuildOutcome.SUCCESS or (
        report.architecture is not None and not report.architecture.passed
    ) or (
        report.acceptance is not None and not report.acceptance.passed
    ):
        failures = [
            f"[sandbox:{phase.phase.value}] "
            f"{phase.error_code or phase.outcome.value}"
            for phase in report.phases
            if phase.outcome != BuildOutcome.SUCCESS
        ] or [f"[sandbox] {report.error_code or report.outcome.value}"]
        if report.architecture is not None:
            failures.extend(
                f"[architecture:{item.rule_id}] {item.path}:{item.line} → {item.dependency}; {item.remediation}"
                for item in report.architecture.findings[:10]
            )
        if report.acceptance is not None:
            failures.extend(
                f"[acceptance:{case.case_id}] {case.criterion}: saída ou execução não atende ao contrato."
                for case in report.acceptance.cases if not case.passed
            )
        updates.update(
            approved=False,
            score=min(evaluation.score, 0.4),
            failures=[*evaluation.failures, *failures],
            required_changes=[
                *evaluation.required_changes,
                "As fases obrigatórias do perfil de build reprovaram. "
                "Corrija a causa usando a evidência sanitizada abaixo:",
                *failures,
            ],
        )
    return evaluation.model_copy(update=updates)


def with_delivery_section(final_output: str | None, result: DeliveryResult) -> str:
    lines = [final_output or "", "", "## Entrega"]
    if result.url:
        lines.append(f"Pull request: {result.url} (branch `{result.branch}`)")
    if result.commit_sha:
        lines.append(f"Commit: `{result.commit_sha[:12]}`")
    lines.append(f"CI: {result.ci_state}")
    if result.error:
        lines.append(f"Erro: {result.error}")
    for line in result.failures[:10]:
        lines.append(f"- {line}")
    return "\n".join(lines).strip()


def build_validation_section(report: BuildRunResult) -> str:
    lines = ["## Validação em sandbox", f"Resultado: {report.outcome.value}"]
    if report.acceptance is None:
        lines.append("Aceitação independente: sem evidência; testes do repositório não comprovam os requisitos por si só.")
    else:
        acceptance = report.acceptance
        lines.append(
            f"Aceitação independente: {'aprovada' if acceptance.passed else 'reprovada'}; "
            f"casos={len(acceptance.cases)}; critérios declarados={len(set(acceptance.required_criteria))}; "
            f"suite={acceptance.suite_digest}"
        )
        for case in acceptance.cases:
            lines.append(f"- {case.case_id}: {'passou' if case.passed else 'falhou'} — {case.criterion}")
    if report.architecture is not None:
        architecture = report.architecture
        lines.append(
            f"Arquitetura: {'aprovada' if architecture.passed else 'reprovada'}; arquivos={architecture.files_checked}"
        )
        for finding in architecture.findings[:10]:
            lines.append(
                f"- {finding.rule_id}: {finding.path}:{finding.line} → {finding.dependency}; {finding.remediation}"
            )
    for phase in report.phases:
        detail = phase.error_code or phase.outcome.value
        lines.append(
            f"- {phase.phase.value}: {phase.outcome.value} "
            f"({phase.duration_seconds:.3f}s; {detail})"
        )
    if report.error_code and not report.phases:
        lines.append(f"- erro: {report.error_code}")
    return "\n".join(lines)
