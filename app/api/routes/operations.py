from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.auth import (
    AuthenticatedClient,
    record_audit_event,
    require_api_client,
    require_role,
)
from app.api.container import Container

router = APIRouter(tags=["operations"])


def _container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


@router.get("/health")
async def health(request: Request) -> dict[str, str]:
    return {"status": "ok", "environment": request.app.state.settings.environment}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    readiness = await _container(request).workflow_service.readiness()
    payload = {
        "status": "ready" if readiness["ready"] else "not_ready",
        **readiness,
    }
    return JSONResponse(status_code=200 if readiness["ready"] else 503, content=payload)


@router.get("/operations/installation")
async def installation_diagnostics(
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> JSONResponse:
    require_role(client, "admin")
    report = await _container(request).workflow_service.installation_diagnostics()
    status_code = 200 if report["ready"] else 503
    await record_audit_event(
        request,
        action="installation_diagnostics",
        outcome="ready" if report["ready"] else "not_ready",
        client_id=client.client_id,
        status_code=status_code,
    )
    return JSONResponse(status_code=status_code, content=report)


@router.get("/metrics")
async def metrics(request: Request) -> dict[str, object]:
    container = _container(request)
    service_metrics = await container.workflow_service.metrics()
    audit_metrics = await container.audit_log.metrics()
    return {
        "app_name": request.app.state.settings.app_name,
        "environment": request.app.state.settings.environment,
        "audit": audit_metrics,
        **service_metrics,
    }


@router.get("/metrics/prometheus", response_class=PlainTextResponse)
async def prometheus_metrics(request: Request) -> PlainTextResponse:
    runtime = await _container(request).workflow_service.metrics()
    body = request.app.state.http_metrics.render(runtime)
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


@router.get("/audit/events")
async def audit_events(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    client: AuthenticatedClient = Depends(require_api_client),
) -> dict[str, object]:
    require_role(client, "admin")
    events = await _container(request).audit_log.list_recent(
        limit=limit, client_id=client.client_id
    )
    await record_audit_event(
        request,
        action="audit_read",
        outcome="success",
        client_id=client.client_id,
        status_code=200,
        detail=f"limit={limit}",
    )
    return {
        "events": [event.to_dict() for event in events],
        "count": len(events),
        "client_id": client.client_id,
    }
