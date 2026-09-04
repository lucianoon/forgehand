"""Resultados persistíveis do executor de fases, sem recursos de processo."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.models.build import BuildPhaseName
from app.models.architecture import ArchitectureReport


class BuildOutcome(str, Enum):
    SUCCESS = "success"
    COMMAND_FAILURE = "command_failure"
    POLICY_REJECTION = "policy_rejection"
    TIMEOUT = "timeout"
    RESOURCE_LIMIT = "resource_limit"
    CANCELLED = "cancelled"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class SandboxLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_mib: int = Field(default=512, ge=64, le=8192)
    cpus: float = Field(default=1.0, gt=0, le=8, allow_inf_nan=False)
    pids: int = Field(default=128, ge=16, le=1024)
    tmp_mib: int = Field(default=64, ge=8, le=512)
    control_timeout_seconds: float = Field(default=10, gt=0, le=60)


class BuildPhaseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: BuildPhaseName
    outcome: BuildOutcome
    command: tuple[str, ...]
    image: str
    cwd: str
    duration_seconds: float = Field(ge=0)
    exit_code: int | None = None
    stdout: str = Field(default="", max_length=100_000)
    stdout_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stderr: str = Field(default="", max_length=100_000)
    output_truncated: bool = False
    network_enabled: bool = False
    workspace_read_only: bool = False
    cleanup_failed: bool = False
    container_name: str | None = None
    error_code: str | None = None


class AcceptanceCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    case_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    criterion: str
    expected_stdout_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution: BuildPhaseResult

    @property
    def passed(self) -> bool:
        run = self.execution
        return (
            run.outcome == BuildOutcome.SUCCESS and run.exit_code == 0
            and not run.error_code and not run.cleanup_failed
            and not run.output_truncated and not run.network_enabled
            and run.workspace_read_only
            and run.stdout_sha256 == self.expected_stdout_sha256
        )


class AcceptanceReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    suite_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_criteria: tuple[str, ...]
    complete: bool = False
    cases: tuple[AcceptanceCaseResult, ...] = Field(default=(), max_length=8)

    @property
    def passed(self) -> bool:
        return (
            self.complete and bool(self.cases) and bool(self.required_criteria)
            and len({case.case_id for case in self.cases}) == len(self.cases)
            and all(case.passed for case in self.cases)
            and set(self.required_criteria) <= {case.criterion for case in self.cases}
        )


class BuildRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_name: str | None
    profile_digest: str | None
    outcome: BuildOutcome
    phases: tuple[BuildPhaseResult, ...] = ()
    error_code: str | None = None
    architecture: ArchitectureReport | None = None
    acceptance: AcceptanceReport | None = None
