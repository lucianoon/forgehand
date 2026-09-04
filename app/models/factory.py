"""Contratos persistentes do modo fábrica.

Os modelos deste módulo só carregam dados serializáveis. Recursos de processo
(runners, clientes GitHub e runtimes) são reconstruídos a partir deles quando
um worker retoma um workflow.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)


class WorkOrderSourceKind(str, Enum):
    DIRECT = "direct"
    GITHUB_ISSUE = "github_issue"


class DirectWorkOrderSource(BaseModel):
    kind: Literal[WorkOrderSourceKind.DIRECT] = WorkOrderSourceKind.DIRECT


class GitHubIssueSnapshot(BaseModel):
    url: HttpUrl
    number: int = Field(gt=0)
    title: str = Field(min_length=1)
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    repository: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    author: str = Field(min_length=1)
    updated_at: datetime
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GitHubIssueWorkOrderSource(BaseModel):
    kind: Literal[WorkOrderSourceKind.GITHUB_ISSUE] = WorkOrderSourceKind.GITHUB_ISSUE
    snapshot: GitHubIssueSnapshot


WorkOrderSource = Annotated[
    DirectWorkOrderSource | GitHubIssueWorkOrderSource,
    Field(discriminator="kind"),
]


class RepositoryTarget(BaseModel):
    full_name: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    base_ref: str = Field(default="main", min_length=1, max_length=255)
    scm_host: str = Field(default="github.com", min_length=1)
    expected_base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")

    @field_validator("scm_host")
    @classmethod
    def _normalize_host(cls, value: str) -> str:
        host = value.strip().lower().rstrip(".")
        if "://" in host or "/" in host or not host:
            raise ValueError("scm_host deve ser somente um hostname.")
        return host


class WorkOrderLimits(BaseModel):
    max_tokens: int = Field(default=500_000, gt=0)
    max_cost_usd: float = Field(default=5.0, gt=0)
    max_iterations: int = Field(default=3, ge=1)
    max_wall_clock_seconds: int = Field(default=1800, gt=0)


class BuildProfileSelection(BaseModel):
    requested_profile: str | None = Field(default=None, min_length=1)
    selected_profile: str | None = Field(default=None, min_length=1)
    selection_reason: (
        Literal["explicit", "repository_mapping", "detected", "unsupported"] | None
    ) = None
    phases: list[str] = Field(default_factory=list)
    profile_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    architecture_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    acceptance_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    acceptance_cases: dict[str, str] = Field(default_factory=dict)
    acceptance_criteria: list[str] = Field(default_factory=list)
    unsupported_reason: str | None = None

    @model_validator(mode="after")
    def _selection_is_coherent(self) -> "BuildProfileSelection":
        if self.selection_reason == "unsupported" and self.selected_profile:
            raise ValueError("perfil unsupported não pode ter selected_profile.")
        if self.selected_profile and self.selection_reason is None:
            raise ValueError("selected_profile exige selection_reason.")
        return self


class DeliveryPolicy(BaseModel):
    create_pull_request: bool = True
    wait_for_checks: bool = True
    checks_timeout_seconds: int = Field(default=900, ge=30, le=7200)
    require_human_merge: bool = True

    @model_validator(mode="after")
    def _human_merge_is_mandatory(self) -> "DeliveryPolicy":
        if not self.create_pull_request:
            raise ValueError("o MVP exige entrega por pull request.")
        if not self.require_human_merge:
            raise ValueError("o MVP exige merge humano.")
        return self


class WorkOrder(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    source: WorkOrderSource
    repository: RepositoryTarget
    requested_outcome: str = Field(min_length=10)
    acceptance_criteria: list[str] = Field(min_length=1)
    limits: WorkOrderLimits = Field(default_factory=WorkOrderLimits)
    build_profile: BuildProfileSelection = Field(default_factory=BuildProfileSelection)
    delivery_policy: DeliveryPolicy = Field(default_factory=DeliveryPolicy)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _issue_repository_matches_target(self) -> "WorkOrder":
        if isinstance(self.source, GitHubIssueWorkOrderSource):
            issue_repository = self.source.snapshot.repository.lower()
            if issue_repository != self.repository.full_name.lower():
                raise ValueError(
                    "issue e work order devem apontar ao mesmo repositório."
                )
        return self


class WorkspaceLifecycle(str, Enum):
    REQUESTED = "requested"
    PROVISIONING = "provisioning"
    READY = "ready"
    ACTIVE = "active"
    RETAINED = "retained"
    RELEASING = "releasing"
    RELEASED = "released"
    FAILED = "failed"


class WorkspaceRetention(BaseModel):
    retain_until: AwareDatetime | None = None
    reason: str | None = None


class WorkspaceLease(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    workflow_id: str = Field(min_length=1)
    repository: RepositoryTarget
    local_path: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    base_sha: str = Field(pattern=r"^[0-9a-fA-F]{40,64}$")
    state: WorkspaceLifecycle = WorkspaceLifecycle.REQUESTED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    retention: WorkspaceRetention = Field(default_factory=WorkspaceRetention)
    failure_reason: str | None = None

    @field_validator("local_path")
    @classmethod
    def _path_must_be_absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("local_path deve ser absoluto.")
        return value


class FactoryStage(str, Enum):
    INTAKE = "intake"
    PROVISIONING = "provisioning"
    STRATEGY_SELECTION = "strategy_selection"
    IMPLEMENTATION = "implementation"
    VALIDATION = "validation"
    DELIVERY = "delivery"
    READY_FOR_HUMAN_REVIEW = "ready_for_human_review"
    CLEANUP = "cleanup"
