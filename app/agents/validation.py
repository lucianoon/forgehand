from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from pydantic import BaseModel

from app.models.task import AgentTask, Capability


class ValidationSignal(BaseModel):
    name: str  # "pytest" | "ruff" | "mypy" | "sandbox" | ...
    passed: bool | None = None
    details: str = ""
    command: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""


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
