"""Operator-approved, bounded product evolution contracts."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

Text = Annotated[str, Field(min_length=1, max_length=500)]


class DeliveryFeature(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=10, max_length=1500)
    acceptance_criteria: list[Text] = Field(min_length=1, max_length=8)


class ProductDeliveryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = Field(pattern=r"^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$", max_length=200)
    base_ref: str = Field(default="main", min_length=1, max_length=150)
    build_profile: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    features: list[DeliveryFeature] = Field(min_length=1, max_length=20)
    decisions: list[Text] = Field(default_factory=list, max_length=20)
    preservation_constraints: list[Text] = Field(min_length=1, max_length=12)

    @field_validator("repository")
    @classmethod
    def repository_name(cls, value: str) -> str:
        if value.split("/")[1] in {".", ".."}:
            raise ValueError("Invalid repository name")
        return value.lower()

    @field_validator("base_ref")
    @classmethod
    def branch_name(cls, value: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_./-]*", value):
            raise ValueError("Invalid base branch")
        if ".." in value or "//" in value or value.endswith(("/", ".", ".lock")):
            raise ValueError("Invalid base branch")
        if any(
            part.startswith(".") or part.endswith(".lock") for part in value.split("/")
        ):
            raise ValueError("Invalid base branch")
        return value


class DeliveryRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)


class AppendDelivery(DeliveryRevision):
    features: list[DeliveryFeature] = Field(default_factory=list, max_length=20)
    decisions: list[Text] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.features and not self.decisions:
            raise ValueError("Add a feature or decision")
        return self


class StartDelivery(DeliveryRevision):
    approved: Literal[True]
    max_cost_usd: float = Field(ge=0.01, le=5, allow_inf_nan=False)
    max_tokens: int = Field(default=100_000, ge=1000, le=500_000)


class RecoverDelivery(DeliveryRevision):
    approved: Literal[True]
    workflow_id: UUID


class MergeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pull_request_number: int = Field(gt=0)
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    merge_commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    base_sha: str = Field(pattern=r"^[a-f0-9]{40}$")


class DeliveryPreflightCheck(BaseModel):
    code: str
    status: Literal["pass", "block", "warning"]
    message: str


class DeliveryPreflight(BaseModel):
    product_id: str
    revision: int
    checks: list[DeliveryPreflightCheck]
    not_checked: list[str]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def can_start(self) -> bool:
        return bool(self.checks) and all(
            check.status != "block" for check in self.checks
        )
