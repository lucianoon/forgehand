"""Rotas de workflows.

POST /workflows           → 202: cria e dispara em background
GET  /workflows/{id}      → estado atual lido do checkpointer
POST /workflows/{id}/decision → retoma interrupt do human_gate
"""

from __future__ import annotations

import os
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.auth import (
    AuthenticatedClient,
    ensure_project_access,
    ensure_workflow_access,
    record_audit_event,
    require_api_client,
    require_role,
)
from app.api.container import Container
from app.api.service import (
    NoPendingDecision,
    WorkflowAlreadyTerminal,
    WorkflowNotFound,
)
from app.graph.state import WorkflowBudget, WorkflowPhase
from app.infrastructure.scm import (
    GitHubSCMClient,
    SCMError,
    collect_publishable_changes,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])

# Fases internas → status externo (contrato da API não expõe o grafo)
_PHASE_STATUS: dict[WorkflowPhase, str] = {
    WorkflowPhase.QUEUED: "running",
    WorkflowPhase.LOADING_CONTEXT: "running",
    WorkflowPhase.PLANNING: "running",
    WorkflowPhase.EXECUTING: "running",
    WorkflowPhase.EVALUATING: "running",
    WorkflowPhase.REPLANNING: "running",
    WorkflowPhase.SYNTHESIZING: "running",
    WorkflowPhase.PERSISTING: "running",
    WorkflowPhase.AWAITING_HUMAN: "awaiting_decision",
    WorkflowPhase.COMPLETED: "completed",
    WorkflowPhase.FAILED: "failed",
    WorkflowPhase.CANCELLED: "cancelled",
}


class BudgetIn(BaseModel):
    max_tokens: int | None = Field(default=None, gt=0)
    max_cost_usd: float | None = Field(default=None, gt=0)
    max_iterations: int | None = Field(default=None, ge=1)


class CreateWorkflowRequest(BaseModel):
    project_id: str = Field(min_length=1)
    request: str = Field(min_length=10)
    budget: BudgetIn | None = None
    # acceptance_criteria do payload original: anexados à requisição para o
    # planner distribuí-los entre as tarefas
    acceptance_criteria: list[str] = Field(default_factory=list)


class CreateWorkflowResponse(BaseModel):
    workflow_id: str
    status: str
    current_stage: str


class TaskSummary(BaseModel):
    id: str
    title: str
    capability: str
    status: str
    attempts: int


class WorkflowStatusResponse(BaseModel):
    workflow_id: str
    status: str
    current_stage: str
    iteration: int
    usage: dict[str, float]
    tasks: list[TaskSummary]
    pending_decision: dict[str, Any] | None = None
    final_output: str | None = None
    error: str | None = None


class DecisionRequest(BaseModel):
    decision: Literal["accept_partial", "retry", "abort"]


class PullRequestRequest(BaseModel):
    repository: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    base_branch: str = Field(default="main", min_length=1)
    head_branch: str | None = Field(default=None, min_length=1)
    title: str | None = None


def _container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


def _to_response(data: dict[str, Any]) -> WorkflowStatusResponse:
    phase: WorkflowPhase = data["phase"]
    # O interrupt() pausa ANTES de human_gate retornar seu update de fase,
    # então a fase persistida ainda é a anterior. O interrupt pendente é o
    # sinal autoritativo de que há decisão aguardando.
    if data["pending_decision"] is not None:
        status_str = "awaiting_decision"
        stage = WorkflowPhase.AWAITING_HUMAN.value
    else:
        status_str = _PHASE_STATUS[phase]
        stage = phase.value
    return WorkflowStatusResponse(
        workflow_id=data["workflow_id"],
        status=status_str,
        current_stage=stage,
        iteration=data["iteration"],
        usage={k: float(v) for k, v in (data["usage"] or {}).items()},
        tasks=[TaskSummary(**t) for t in data["tasks"]],
        pending_decision=data["pending_decision"],
        final_output=data["final_output"],
        error=data.get("error"),
    )


@router.post(
    "", status_code=status.HTTP_202_ACCEPTED, response_model=CreateWorkflowResponse
)
async def create_workflow(
    body: CreateWorkflowRequest,
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> CreateWorkflowResponse:
    require_role(client, "operator")
    service = _container(request).workflow_service
    await ensure_project_access(request, client, body.project_id)

    full_request = body.request
    if body.acceptance_criteria:
        criteria = "\n".join(f"- {c}" for c in body.acceptance_criteria)
        full_request += f"\n\nCritérios de aceitação do solicitante:\n{criteria}"

    budget = None
    if body.budget is not None:
        overrides = body.budget.model_dump(exclude_none=True)
        budget = WorkflowBudget(**overrides) if overrides else None

    workflow_id = await service.start(
        body.project_id, full_request, budget, owner_client_id=client.client_id
    )
    await record_audit_event(
        request,
        action="workflow_create",
        outcome="accepted",
        client_id=client.client_id,
        project_id=body.project_id,
        workflow_id=workflow_id,
        status_code=status.HTTP_202_ACCEPTED,
    )
    return CreateWorkflowResponse(
        workflow_id=workflow_id, status="running", current_stage="queued"
    )


@router.get("", response_model=list[WorkflowStatusResponse])
async def list_workflows(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    project_id: str | None = Query(default=None),
    client: AuthenticatedClient = Depends(require_api_client),
) -> list[WorkflowStatusResponse]:
    require_role(client, "viewer")
    if project_id is not None:
        await ensure_project_access(request, client, project_id)
    container = _container(request)
    events = await container.audit_log.list_recent(
        limit=min(limit * 20, 2000), client_id=client.client_id
    )
    workflow_ids: list[str] = []
    for event in events:
        if (
            event.action != "workflow_create"
            or event.outcome != "accepted"
            or event.client_id != client.client_id
            or event.workflow_id is None
            or (project_id is not None and event.project_id != project_id)
            or event.workflow_id in workflow_ids
        ):
            continue
        workflow_ids.append(event.workflow_id)
        if len(workflow_ids) >= limit:
            break

    workflows: list[WorkflowStatusResponse] = []
    for workflow_id in workflow_ids:
        try:
            workflows.append(
                _to_response(await container.workflow_service.get(workflow_id))
            )
        except WorkflowNotFound:
            continue
    return workflows


@router.get("/{workflow_id}", response_model=WorkflowStatusResponse)
async def get_workflow(
    workflow_id: str,
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> WorkflowStatusResponse:
    service = _container(request).workflow_service
    try:
        access = await service.get_access_context(workflow_id)
        await ensure_workflow_access(request, client, access)
        response = _to_response(await service.get(workflow_id))
        await record_audit_event(
            request,
            action="workflow_get",
            outcome="success",
            client_id=client.client_id,
            project_id=access.project_id,
            workflow_id=workflow_id,
            status_code=status.HTTP_200_OK,
        )
        return response
    except WorkflowNotFound:
        await record_audit_event(
            request,
            action="workflow_get",
            outcome="not_found",
            client_id=client.client_id,
            workflow_id=workflow_id,
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workflow não encontrado.",
        )
        raise HTTPException(status_code=404, detail="Workflow não encontrado.")


@router.post("/{workflow_id}/decision", status_code=status.HTTP_202_ACCEPTED)
async def decide(
    workflow_id: str,
    body: DecisionRequest,
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> dict[str, str]:
    require_role(client, "approver")
    service = _container(request).workflow_service
    try:
        access = await service.get_access_context(workflow_id)
        await ensure_workflow_access(request, client, access)
        await service.decide(workflow_id, body.decision)
    except WorkflowNotFound:
        await record_audit_event(
            request,
            action="workflow_decision",
            outcome="not_found",
            client_id=client.client_id,
            workflow_id=workflow_id,
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workflow não encontrado.",
        )
        raise HTTPException(status_code=404, detail="Workflow não encontrado.")
    except NoPendingDecision as exc:
        await record_audit_event(
            request,
            action="workflow_decision",
            outcome="conflict",
            client_id=client.client_id,
            workflow_id=workflow_id,
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
        raise HTTPException(status_code=409, detail=str(exc))
    await record_audit_event(
        request,
        action="workflow_decision",
        outcome="accepted",
        client_id=client.client_id,
        project_id=access.project_id,
        workflow_id=workflow_id,
        status_code=status.HTTP_202_ACCEPTED,
        detail=body.decision,
    )
    return {"workflow_id": workflow_id, "status": "resuming"}


@router.post("/{workflow_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_workflow(
    workflow_id: str,
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> dict[str, str]:
    require_role(client, "operator")
    service = _container(request).workflow_service
    try:
        access = await service.get_access_context(workflow_id)
        await ensure_workflow_access(request, client, access)
        await service.cancel(workflow_id)
    except WorkflowNotFound:
        raise HTTPException(status_code=404, detail="Workflow não encontrado.")
    except WorkflowAlreadyTerminal as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workflow já está em estado terminal.",
        ) from exc
    await record_audit_event(
        request,
        action="workflow_cancel",
        outcome="accepted",
        client_id=client.client_id,
        project_id=access.project_id,
        workflow_id=workflow_id,
        status_code=status.HTTP_202_ACCEPTED,
    )
    return {"workflow_id": workflow_id, "status": "cancelled"}


@router.get("/{workflow_id}/details")
async def workflow_details(
    workflow_id: str,
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> dict[str, Any]:
    service = _container(request).workflow_service
    access = await service.get_access_context(workflow_id)
    await ensure_workflow_access(request, client, access)
    return await service.get_details(workflow_id)


@router.post("/{workflow_id}/pull-request", status_code=status.HTTP_201_CREATED)
async def publish_pull_request(
    workflow_id: str,
    body: PullRequestRequest,
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> dict[str, Any]:
    require_role(client, "approver")
    service = _container(request).workflow_service
    access = await service.get_access_context(workflow_id)
    await ensure_workflow_access(request, client, access)
    details = await service.get_details(workflow_id)
    files, deletions = collect_publishable_changes(details["tasks"])
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GITHUB_TOKEN não configurado.",
        )
    client_scm = GitHubSCMClient(token)
    head = body.head_branch or f"forgehand/{workflow_id[:12]}"
    try:
        pull = await client_scm.publish_pull_request(
            repository=body.repository,
            base_branch=body.base_branch,
            head_branch=head,
            title=body.title or f"Forgehand: {details['project_id']}",
            body=f"Entrega auditável do workflow `{workflow_id}`.",
            files=files,
            deletions=deletions,
        )
    except (SCMError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client_scm.close()
    await record_audit_event(
        request,
        action="pull_request_create",
        outcome="success",
        client_id=client.client_id,
        project_id=access.project_id,
        workflow_id=workflow_id,
        status_code=201,
        detail=pull.url,
    )
    return {"number": pull.number, "url": pull.url, "branch": pull.branch}
