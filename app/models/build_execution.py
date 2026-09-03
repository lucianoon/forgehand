"""Resultados persistíveis do executor de fases, sem recursos de processo."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.models.build import BuildPhaseName


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
    stderr: str = Field(default="", max_length=100_000)
    output_truncated: bool = False
    network_enabled: bool = False
    cleanup_failed: bool = False
    container_name: str | None = None
    error_code: str | None = None


class BuildRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_name: str | None
    profile_digest: str | None
    outcome: BuildOutcome
    phases: tuple[BuildPhaseResult, ...] = ()
    error_code: str | None = None
