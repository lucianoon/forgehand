from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from pydantic import BaseModel

from app.models.build_execution import BuildOutcome
from app.models.task import AgentTask, Capability, CriterionKind


class ValidationSignal(BaseModel):
    name: str  # "pytest" | "ruff" | "mypy" | "sandbox" | ...
    passed: bool | None = None
    details: str = ""
    command: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""


def build_validation_signals(
    task: AgentTask, *, required: bool = False
) -> list[ValidationSignal]:
    """Use only the current attempt's runtime-owned sandbox evidence.

    A successful build is not evidence that tests, lint or types ran. Missing
    required phases fail closed in factory mode; executor text, workspace
    feedback and earlier attempts cannot substitute for a fresh report.
    """
    report = task.attempts[-1].build_validation if task.attempts else None
    if report is None and not required:
        return []
    kinds = {criterion.kind for criterion in task.acceptance_criteria}
    phases = {phase.phase.value: phase for phase in report.phases} if report else {}
    failed_phases = [
        phase.phase.value for phase in phases.values()
        if phase.outcome != BuildOutcome.SUCCESS or phase.exit_code != 0
        or phase.cleanup_failed or phase.error_code
    ]
    detail = "relatório sandbox ausente na tentativa atual"
    if report is not None:
        detail = report.error_code or report.outcome.value
        if not report.phases:
            detail = "relatório sandbox sem fases executadas"
        elif len(phases) != len(report.phases):
            detail = "relatório sandbox contém fases duplicadas"
        elif failed_phases:
            detail = "fases sandbox sem sucesso: " + ", ".join(failed_phases)
        elif report.architecture is not None and not report.architecture.passed:
            detail = "arquitetura reprovada no sandbox"
        elif report.acceptance is not None and not report.acceptance.passed:
            detail = "aceitação independente reprovada no sandbox"
    signals = [ValidationSignal(
        name="sandbox",
        passed=bool(
            report is not None
            and report.outcome == BuildOutcome.SUCCESS
            and not report.error_code
            and report.phases
            and len(phases) == len(report.phases)
            and not failed_phases
            and (report.architecture is None or report.architecture.passed)
            and (report.acceptance is None or report.acceptance.passed)
        ),
        details=detail,
    )]
    for kind, phase_name, signal_name in (
        (CriterionKind.TESTS_PASS, "test", "pytest"),
        (CriterionKind.LINT_PASS, "lint", "ruff"),
        (CriterionKind.TYPES_PASS, "types", "mypy"),
    ):
        phase = phases.get(phase_name)
        if phase is None:
            if kind in kinds:
                signals.append(ValidationSignal(
                    name=signal_name,
                    passed=False,
                    details=f"fase sandbox `{phase_name}` ausente na tentativa atual",
                ))
            continue
        signals.append(ValidationSignal(
            name=signal_name,
            passed=(
                phase.outcome == BuildOutcome.SUCCESS
                and phase.exit_code == 0
                and not phase.cleanup_failed
                and not phase.error_code
            ),
            details=f"sandbox:{phase_name}: {phase.error_code or phase.outcome.value}",
            command=" ".join(phase.command),
            exit_code=phase.exit_code,
            stdout=phase.stdout,
            stderr=phase.stderr,
        ))
    return signals


def format_validation_feedback(
    feedback: object,
    *,
    max_field_chars: int = 240,
) -> str:
    if not isinstance(feedback, list):
        return ""

    lines: list[str] = []
    for item in feedback:
        signal = _coerce_validation_signal(item)
        if signal is None:
            continue

        status = (
            "passed"
            if signal.passed is True
            else "failed"
            if signal.passed is False
            else "skipped"
        )
        header = f"- {signal.name}: {status}"
        if signal.exit_code is not None:
            header += f" (exit_code={signal.exit_code})"
        lines.append(header)

        if signal.command:
            lines.append(f"  command: {_compact_text(signal.command, max_field_chars)}")
        if signal.details.strip():
            lines.append(f"  details: {_compact_text(signal.details, max_field_chars)}")
        if signal.stdout.strip():
            lines.append(f"  stdout: {_compact_text(signal.stdout, max_field_chars)}")
        if signal.stderr.strip():
            lines.append(f"  stderr: {_compact_text(signal.stderr, max_field_chars)}")
    return "\n".join(lines)


def _coerce_validation_signal(item: object) -> ValidationSignal | None:
    if isinstance(item, ValidationSignal):
        return item
    if isinstance(item, dict):
        return ValidationSignal.model_validate(item)
    return None


def _compact_text(value: str, max_chars: int) -> str:
    compact = " ".join(value.split())
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 3]}..."


class ObjectiveValidator(Protocol):
    name: str

    async def validate(self, task: AgentTask) -> ValidationSignal: ...


class ObjectiveValidationPipeline:
    def __init__(
        self,
        validators: Iterable[ObjectiveValidator],
        capability_pipelines: dict[Capability, list[str]] | None = None,
    ) -> None:
        self._validators_by_name = {
            validator.name: validator for validator in validators
        }
        self._default_order = list(self._validators_by_name)
        self._capability_pipelines = capability_pipelines or {}

    def validators_for_capability(
        self, capability: Capability
    ) -> list[ObjectiveValidator]:
        names = self._capability_pipelines.get(capability, self._default_order)
        return [
            self._validators_by_name[name]
            for name in names
            if name in self._validators_by_name
        ]

    def validators_for_task(self, task: AgentTask) -> list[ObjectiveValidator]:
        return self.validators_for_capability(task.capability)

    async def validate(self, task: AgentTask) -> list[ValidationSignal]:
        return [
            await validator.validate(task)
            for validator in self.validators_for_task(task)
        ]
