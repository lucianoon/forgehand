"""Normalização de entradas externas em ordens de trabalho canônicas."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from app.models.factory import (
    BuildProfileSelection,
    DeliveryPolicy,
    DirectWorkOrderSource,
    GitHubIssueSnapshot,
    GitHubIssueWorkOrderSource,
    RepositoryTarget,
    WorkOrder,
    WorkOrderLimits,
)

_ISSUE_PATH = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>[1-9][0-9]*)/?$"
)


class DirectWorkOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    base_ref: str = Field(default="main", min_length=1, max_length=255)
    scm_host: str = Field(default="github.com", min_length=1)
    expected_base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    requested_outcome: str = Field(min_length=10)
    acceptance_criteria: list[str] = Field(min_length=1)
    limits: WorkOrderLimits = Field(default_factory=WorkOrderLimits)
    build_profile: str | None = Field(default=None, min_length=1)
    delivery_policy: DeliveryPolicy = Field(default_factory=DeliveryPolicy)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)


class GitHubIssueWorkOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_url: str = Field(min_length=1)
    base_ref: str = Field(default="main", min_length=1, max_length=255)
    acceptance_criteria: list[str] = Field(min_length=1)
    limits: WorkOrderLimits = Field(default_factory=WorkOrderLimits)
    build_profile: str | None = Field(default=None, min_length=1)
    delivery_policy: DeliveryPolicy = Field(default_factory=DeliveryPolicy)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)


def parse_github_issue_url(url: str, approved_hosts: list[str]) -> tuple[str, int, str]:
    """Valida sem rede e devolve repository, número e hostname canônicos."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = {item.lower().rstrip(".") for item in approved_hosts}
    if parsed.scheme != "https" or host not in allowed:
        raise ValueError("issue deve usar HTTPS em um host SCM aprovado.")
    if parsed.username or parsed.password or parsed.port is not None:
        raise ValueError("issue URL não pode conter credenciais ou porta.")
    if parsed.query or parsed.fragment:
        raise ValueError("issue URL não pode conter query ou fragmento.")
    match = _ISSUE_PATH.fullmatch(parsed.path)
    if match is None:
        raise ValueError("issue URL deve usar /owner/repository/issues/number.")
    repository = f"{match.group('owner')}/{match.group('repo')}"
    return repository, int(match.group("number")), host


def normalize_direct_work_order(value: DirectWorkOrderInput) -> WorkOrder:
    """Constrói a representação persistida antes de fila, workspace ou LLM."""
    return WorkOrder(
        source=DirectWorkOrderSource(),
        repository=RepositoryTarget(
            full_name=value.repository,
            base_ref=value.base_ref,
            scm_host=value.scm_host,
            expected_base_sha=value.expected_base_sha,
        ),
        requested_outcome=value.requested_outcome,
        acceptance_criteria=value.acceptance_criteria,
        limits=value.limits,
        build_profile=BuildProfileSelection(
            requested_profile=value.build_profile,
        ),
        delivery_policy=value.delivery_policy,
        idempotency_key=value.idempotency_key,
    )


def normalize_github_issue_work_order(
    value: GitHubIssueWorkOrderInput,
    snapshot: GitHubIssueSnapshot,
) -> WorkOrder:
    return WorkOrder(
        source=GitHubIssueWorkOrderSource(snapshot=snapshot),
        repository=RepositoryTarget(
            full_name=snapshot.repository,
            base_ref=value.base_ref,
            scm_host=snapshot.url.host or "github.com",
        ),
        requested_outcome=f"{snapshot.title}\n\n{snapshot.body}".strip(),
        acceptance_criteria=value.acceptance_criteria,
        limits=value.limits,
        build_profile=BuildProfileSelection(
            requested_profile=value.build_profile,
        ),
        delivery_policy=value.delivery_policy,
        idempotency_key=value.idempotency_key,
    )


def planner_request(order: WorkOrder) -> str:
    criteria = "\n".join(f"- {item}" for item in order.acceptance_criteria)
    return (
        f"{order.requested_outcome}\n\n"
        f"Critérios de aceitação do solicitante:\n{criteria}"
    )
