"""Operator-owned Python import boundaries and persistable scan evidence."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ModulePrefix = Annotated[
    str, Field(pattern=r"^[a-zA-Z_]\w*(\.[a-zA-Z_]\w*)*$", max_length=160)
]


class ImportBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    source: ModulePrefix
    forbidden: tuple[ModulePrefix, ...] = Field(min_length=1, max_length=20)
    remediation: str = Field(min_length=10, max_length=300)


class ArchitecturePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    source_roots: tuple[str, ...] = Field(default=(".",), min_length=1, max_length=8)
    rules: tuple[ImportBoundary, ...] = Field(min_length=1, max_length=30)

    @field_validator("source_roots")
    @classmethod
    def roots_are_confined(cls, roots: tuple[str, ...]) -> tuple[str, ...]:
        for root in roots:
            path = PurePosixPath(root)
            if (
                not root
                or len(root) > 160
                or path.is_absolute()
                or ".." in path.parts
                or path.as_posix() != root
                or not re.fullmatch(r"[a-zA-Z0-9_./-]+", root)
            ):
                raise ValueError("Source roots must be normalized relative directories")
        for i, root in enumerate(roots):
            if any(
                PurePosixPath(root).is_relative_to(other)
                or PurePosixPath(other).is_relative_to(root)
                for other in roots[:i]
            ):
                raise ValueError("Source roots must not overlap")
        return roots

    @model_validator(mode="after")
    def unique_rules(self) -> Self:
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("Architecture rule IDs must be unique")
        if len(json.dumps(self.model_dump(mode="json")).encode()) > 16_000:
            raise ValueError("Architecture policy exceeds 16 KB")
        return self

    def fingerprint(self) -> str:
        value = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(value.encode()).hexdigest()


class ArchitectureFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    rule_id: str = Field(max_length=64)
    code: str = Field(max_length=64)
    path: str = Field(max_length=256)
    line: int = Field(default=0, ge=0)
    dependency: str = Field(default="", max_length=256)
    message: str = Field(max_length=300)
    remediation: str = Field(max_length=300)


class ArchitectureReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    complete: bool
    files_checked: int = Field(ge=0)
    findings: tuple[ArchitectureFinding, ...] = Field(default=(), max_length=50)

    @property
    def passed(self) -> bool:
        return self.complete and self.files_checked > 0 and not self.findings
