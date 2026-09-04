"""Explicitly approved incremental product deliveries; reads never dispatch work."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.api.auth import (
    AuthenticatedClient,
    get_container_from_request,
    record_audit_event,
    require_api_client,
    require_role,
)
from app.api.routes.products import owned, studio
from app.factory.product_delivery import IncrementalDelivery
from app.factory.preflight import delivery_preflight
from app.infrastructure.product_store import ProductConflict
from app.infrastructure.scm import (
    GitHubSCMClient,
    SCMError,
    build_token_provider_from_env,
)
from app.models.product_delivery import (
    AppendDelivery,
    DeliveryPreflight,
    ProductDeliveryPlan,
    RecoverDelivery,
    StartDelivery,
)

router = APIRouter(
    prefix="/products/{product_id}/delivery", tags=["product deliveries"]
)


@asynccontextmanager
async def scm_client(required: bool = False) -> AsyncIterator[GitHubSCMClient | None]:
    provider = build_token_provider_from_env()
    if provider is None:
        if required:
            raise HTTPException(
                409, "Configure uma credencial GitHub no servidor antes de executar."
            )
        yield None
        return
    scm = GitHubSCMClient(token_provider=provider)
    try:
        yield scm
    finally:
        await scm.close()


def envelope(request: Request, plan: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "plan": plan,
        "factory_enabled": request.app.state.settings.factory_mode_enabled,
    }


@router.get("")
async def get_plan(
    product_id: UUID,
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> dict[str, Any]:
    await owned(request, client, product_id)
    try:
        plan = await asyncio.to_thread(
            studio(request).deliveries.get, str(product_id), client.client_id
        )
    except KeyError:
        plan = None
    return envelope(request, plan)


@router.put("")
async def create_plan(
    product_id: UUID,
    body: ProductDeliveryPlan,
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> dict[str, Any]:
    require_role(client, "approver")
    product = await owned(request, client, product_id)
    await record_audit_event(
        request,
        action="product_delivery_plan",
        outcome="requested",
        client_id=client.client_id,
        project_id=product["project_id"],
    )
    try:
        plan = await asyncio.to_thread(
            studio(request).deliveries.create, product, client.client_id, body
        )
    except ProductConflict as exc:
        raise HTTPException(409, str(exc)) from None
    return envelope(request, plan)


@router.get("/preflight", response_model=DeliveryPreflight)
async def preflight_delivery(
    product_id: UUID,
    request: Request,
    response: Response,
    client: AuthenticatedClient = Depends(require_api_client),
) -> DeliveryPreflight:
    require_role(client, "approver")
    await owned(request, client, product_id)
    response.headers["Cache-Control"] = "no-store"
    return await preflight_report(product_id, request, client)


async def preflight_report(
    product_id: UUID,
    request: Request,
    client: AuthenticatedClient,
    *,
    recovering: bool = False,
) -> DeliveryPreflight:
    try:
        plan = await asyncio.to_thread(
            studio(request).deliveries.get, str(product_id), client.client_id
        )
    except KeyError:
        raise HTTPException(404, "Plano não encontrado.") from None
    return await delivery_preflight(
        plan,
        request.app.state.settings,
        get_container_from_request(request).workflow_service,
        recovering=recovering,
    )


@router.post("/append")
async def append_plan(
    product_id: UUID,
    body: AppendDelivery,
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> dict[str, Any]:
    require_role(client, "approver")
    product = await owned(request, client, product_id)
    await record_audit_event(
        request,
        action="product_delivery_append",
        outcome="requested",
        client_id=client.client_id,
        project_id=product["project_id"],
    )
    try:
        plan = await asyncio.to_thread(
            studio(request).deliveries.append, str(product_id), client.client_id, body
        )
    except KeyError:
        raise HTTPException(404, "Plano não encontrado.") from None
    except ProductConflict as exc:
        raise HTTPException(409, str(exc)) from None
    return envelope(request, plan)


@router.post("/start", status_code=202)
async def start_delivery(
    product_id: UUID,
    body: StartDelivery,
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> dict[str, Any]:
    require_role(client, "approver")
    product = await owned(request, client, product_id)
    report = await preflight_report(product_id, request, client)
    if report.revision != body.revision:
        raise HTTPException(
            409, "Plano atualizado por outra operação. Atualize antes de continuar."
        )
    if not report.can_start:
        raise HTTPException(
            409,
            {"code": "delivery_preflight_blocked", "preflight": report.model_dump()},
            headers={"Cache-Control": "no-store"},
        )
    service = IncrementalDelivery(
        studio(request).deliveries, get_container_from_request(request).workflow_service
    )
    await record_audit_event(
        request,
        action="product_delivery_start",
        outcome="requested",
        client_id=client.client_id,
        project_id=product["project_id"],
    )
    try:
        async with scm_client(required=True) as scm:
            assert scm is not None
            plan = await service.start(str(product_id), client.client_id, body, scm)
    except KeyError:
        raise HTTPException(404, "Plano não encontrado.") from None
    except ProductConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except (SCMError, httpx.HTTPError, ValueError):
        raise HTTPException(
            409,
            "Não foi possível verificar o repositório/base; confira credencial e histórico.",
        ) from None
    return envelope(request, plan)


@router.post("/recover", status_code=202)
async def recover_delivery(
    product_id: UUID,
    body: RecoverDelivery,
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> dict[str, Any]:
    require_role(client, "approver")
    product = await owned(request, client, product_id)
    report = await preflight_report(product_id, request, client, recovering=True)
    if report.revision != body.revision:
        raise HTTPException(409, "Plano atualizado por outra operação. Atualize antes de continuar.")
    if not report.can_start:
        raise HTTPException(
            409, {"code": "delivery_preflight_blocked", "preflight": report.model_dump()},
            headers={"Cache-Control": "no-store"},
        )
    await record_audit_event(
        request, action="product_delivery_recover", outcome="requested",
        client_id=client.client_id, project_id=product["project_id"],
        workflow_id=str(body.workflow_id),
    )
    service = IncrementalDelivery(
        studio(request).deliveries, get_container_from_request(request).workflow_service
    )
    try:
        plan = await service.recover(str(product_id), client.client_id, body)
    except KeyError:
        raise HTTPException(404, "Tentativa não encontrada.") from None
    except ProductConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError:
        raise HTTPException(409, "Ordem salva inválida; recuperação bloqueada.") from None
    await record_audit_event(
        request, action="product_delivery_recover", outcome="processed",
        client_id=client.client_id, project_id=product["project_id"],
        workflow_id=str(body.workflow_id),
    )
    return envelope(request, plan)


@router.post("/reconcile")
async def reconcile_delivery(
    product_id: UUID,
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> dict[str, Any]:
    require_role(client, "approver")
    product = await owned(request, client, product_id)
    service = IncrementalDelivery(
        studio(request).deliveries, get_container_from_request(request).workflow_service
    )
    await record_audit_event(
        request,
        action="product_delivery_reconcile",
        outcome="requested",
        client_id=client.client_id,
        project_id=product["project_id"],
    )
    try:
        async with scm_client() as scm:
            plan = await service.reconcile(str(product_id), client.client_id, scm)
    except KeyError:
        raise HTTPException(404, "Plano não encontrado.") from None
    except ProductConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except (SCMError, httpx.HTTPError, ValueError):
        raise HTTPException(
            409, "Merge não confirmado: confira PR, commit, branch e credencial GitHub."
        ) from None
    return envelope(request, plan)


@router.get("/context/{workflow_id}")
async def delivery_context(
    product_id: UUID,
    workflow_id: UUID,
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> dict[str, Any]:
    await owned(request, client, product_id)
    try:
        return await asyncio.to_thread(
            studio(request).deliveries.context,
            str(product_id),
            client.client_id,
            str(workflow_id),
        )
    except KeyError:
        raise HTTPException(404, "Contexto não encontrado.") from None
