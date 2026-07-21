"""Autenticação, RBAC e auditoria de acesso — dependências FastAPI.

Tudo aqui é request-scoped: extrai credenciais do request, valida contra as
API keys configuradas (comparação em tempo constante) e registra o evento
de auditoria correspondente antes de liberar ou negar o acesso.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import cast

from fastapi import HTTPException, Request, status

from app.api.container import Container
from app.infrastructure.audit import build_audit_event
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import WorkflowAccessContext


@dataclass(frozen=True)
class AuthenticatedClient:
    client_id: str
    projects: frozenset[str]
    role: str = "admin"

    def can_access_project(self, project_id: str) -> bool:
        return "*" in self.projects or project_id in self.projects

    def can(self, minimum_role: str) -> bool:
        levels = {"viewer": 0, "operator": 1, "approver": 2, "admin": 3}
        return levels.get(self.role, -1) >= levels[minimum_role]


def get_settings_from_request(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_container_from_request(request: Request) -> Container:
    return cast(Container, request.app.state.container)


async def record_audit_event(
    request: Request,
    *,
    action: str,
    outcome: str,
    client_id: str | None = None,
    project_id: str | None = None,
    workflow_id: str | None = None,
    status_code: int | None = None,
    detail: str | None = None,
) -> None:
    container = get_container_from_request(request)
    await container.audit_log.record(
        build_audit_event(
            action=action,
            outcome=outcome,
            client_id=client_id,
            project_id=project_id,
            workflow_id=workflow_id,
            request_path=str(request.url.path),
            request_method=request.method,
            status_code=status_code,
            detail=detail,
            remote_addr=request.client.host if request.client is not None else None,
        )
    )


async def require_api_client(request: Request) -> AuthenticatedClient:
    settings = get_settings_from_request(request)
    api_key = request.headers.get("X-API-Key")
    if api_key is None:
        container = getattr(request.app.state, "container", None)
        if container is not None:
            await record_audit_event(
                request,
                action="auth",
                outcome="missing_api_key",
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key ausente.",
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key ausente.",
        )
    for configured_key, config in settings.api_keys.items():
        if secrets.compare_digest(api_key, configured_key):
            container = getattr(request.app.state, "container", None)
            if container is not None:
                await record_audit_event(
                    request,
                    action="auth",
                    outcome="authenticated",
                    client_id=config.client_id,
                    status_code=status.HTTP_200_OK,
                )
            return AuthenticatedClient(
                client_id=config.client_id,
                projects=frozenset(config.projects),
                role=config.role,
            )
    container = getattr(request.app.state, "container", None)
    if container is not None:
        await record_audit_event(
            request,
            action="auth",
            outcome="invalid_api_key",
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key inválida.",
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="API key inválida.",
    )


async def ensure_project_access(
    request: Request, client: AuthenticatedClient, project_id: str
) -> None:
    if client.can_access_project(project_id):
        return
    await record_audit_event(
        request,
        action="project_access",
        outcome="forbidden",
        client_id=client.client_id,
        project_id=project_id,
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Cliente não autorizado para este projeto.",
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Cliente não autorizado para este projeto.",
    )


def require_role(client: AuthenticatedClient, minimum_role: str) -> None:
    if not client.can(minimum_role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Operação exige role {minimum_role}.",
        )


async def ensure_workflow_access(
    request: Request, client: AuthenticatedClient, access: WorkflowAccessContext
) -> None:
    if client.client_id != access.owner_client_id:
        await record_audit_event(
            request,
            action="workflow_access",
            outcome="forbidden",
            client_id=client.client_id,
            project_id=access.project_id,
            workflow_id=access.workflow_id,
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cliente não autorizado para este workflow.",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cliente não autorizado para este workflow.",
        )
    await ensure_project_access(request, client, access.project_id)
