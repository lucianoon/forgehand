"""Authenticated studio API; generated artifacts are never served as privileged HTML."""
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from app.agents.product import ProductStudio, demo_archive, demo_document
from app.agents.product_package import fullstack_archive
from app.api.auth import (AuthenticatedClient, ensure_project_access, get_container_from_request,
                          record_audit_event, require_api_client, require_role)
from app.infrastructure.product_store import ProductConflict
from app.models.product import DemoApp, ProductBrief, ProductIdea

router = APIRouter(prefix="/products", tags=["product studio"])


def studio(request: Request) -> ProductStudio:
    service = get_container_from_request(request).product_studio
    if service is None:
        raise HTTPException(503, "Estúdio desativado. Configure PRODUCT_STUDIO_ENABLED=true.")
    return service


async def owned(request: Request, client: AuthenticatedClient, product_id: UUID) -> dict[str, Any]:
    try:
        product = studio(request).store.get(str(product_id), client.client_id)
    except KeyError:
        raise HTTPException(404, "Projeto não encontrado.") from None
    await ensure_project_access(request, client, product["project_id"])
    return product


@router.post("", status_code=201)
async def create_product(body: ProductIdea, request: Request,
                         client: AuthenticatedClient = Depends(require_api_client)) -> dict[str, Any]:
    require_role(client, "operator")
    await ensure_project_access(request, client, body.project_id)
    await record_audit_event(request, action="product_create", outcome="requested",
                             client_id=client.client_id, project_id=body.project_id)
    try:
        return await studio(request).create(client.client_id, body)
    except ProductConflict as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("")
async def list_products(request: Request, project_id: str,
                        client: AuthenticatedClient = Depends(require_api_client)) -> list[dict[str, Any]]:
    await ensure_project_access(request, client, project_id)
    return [{key: value for key, value in product.items() if key != "app"}
            for product in studio(request).store.list(client.client_id, project_id)]


@router.get("/{product_id}")
async def get_product(product_id: UUID, request: Request,
                      client: AuthenticatedClient = Depends(require_api_client)) -> dict[str, Any]:
    return await owned(request, client, product_id)


class Approval(BaseModel):
    brief: ProductBrief


@router.post("/{product_id}/approve")
async def approve_product(product_id: UUID, body: Approval, request: Request,
                          client: AuthenticatedClient = Depends(require_api_client)) -> dict[str, Any]:
    require_role(client, "approver")
    product = await owned(request, client, product_id)
    await record_audit_event(request, action="product_approve", outcome="requested",
                             client_id=client.client_id, project_id=product["project_id"])
    try:
        return await studio(request).approve(client.client_id, str(product_id), body.brief)
    except ProductConflict as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/{product_id}/preview")
async def preview(product_id: UUID, request: Request,
                  client: AuthenticatedClient = Depends(require_api_client)) -> dict[str, str]:
    product = await owned(request, client, product_id)
    if product["status"] != "ready_for_preview":
        raise HTTPException(409, "A aplicação ainda não está pronta para experimentar.")
    # JSON only. The studio installs this into an opaque sandboxed srcdoc.
    return {"document": demo_document(DemoApp.model_validate(product["app"]))}


@router.get("/{product_id}/download")
async def download(product_id: UUID, request: Request,
                   client: AuthenticatedClient = Depends(require_api_client)) -> Response:
    product = await owned(request, client, product_id)
    if product["status"] != "ready_for_preview":
        raise HTTPException(409, "A aplicação ainda não está pronta.")
    return Response(demo_archive(product), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="forgehand-demo.zip"',
                             "Cache-Control": "no-store"})


@router.get("/{product_id}/fullstack")
async def download_fullstack(product_id: UUID, request: Request,
                             client: AuthenticatedClient = Depends(require_api_client)) -> Response:
    product = await owned(request, client, product_id)
    if product["status"] != "ready_for_preview":
        raise HTTPException(409, "Aprove e gere o modelo antes de exportar.")
    return Response(fullstack_archive(product), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="forgehand-fullstack.zip"',
                             "Cache-Control": "no-store"})
