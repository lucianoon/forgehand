"""Trusted acceptance evidence checks, shared by graph and publication gates."""

from typing import Any

from app.models.build_execution import AcceptanceReport
from app.models.factory import BuildProfileSelection


def acceptance_verified(
    report: AcceptanceReport | None, selection: BuildProfileSelection
) -> bool:
    if selection.acceptance_digest is None:
        return (
            report is None
            and not selection.acceptance_cases
            and not selection.acceptance_criteria
        )
    return bool(
        report is not None
        and report.passed
        and report.suite_digest == selection.acceptance_digest
        and list(report.required_criteria) == selection.acceptance_criteria
        and {case.case_id: case.case_digest for case in report.cases}
        == selection.acceptance_cases
        and len(report.cases) == len(selection.acceptance_cases)
    )


def acceptance_feedback(report: AcceptanceReport) -> list[dict[str, Any]]:
    return [
        {
            "name": f"acceptance:{case.case_id}",
            "passed": case.passed,
            "command": " ".join(case.execution.command),
            "exit_code": case.execution.exit_code,
            "details": case.criterion,
            "stdout": case.execution.stdout,
            "stderr": case.execution.error_code
            or (
                ""
                if case.passed
                else "Comportamento divergente do contrato aprovado; saída deve corresponder exatamente."
            ),
            "duration_seconds": case.execution.duration_seconds,
            "output_truncated": case.execution.output_truncated,
        }
        for case in report.cases
    ]
