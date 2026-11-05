"""Rotas e adaptador de entrada (Ingress) do Super-Agent.

Endpoints:
- POST /superagent/github/webhook        → Ingress de eventos GitHub (Issues, PR comments, Reviews)
- GET  /superagent/tickets               → Lista tickets avaliados pelo Router (incluindo modo observe)
- POST /superagent/tickets/{id}/approve  → Aprova um candidato `observed` e despacha para um Worker
- GET  /superagent/subscriptions         → Lista assinaturas ativas de Threads (Subscription Direct)
- GET  /superagent/outbox                → Lista mensagens da fila Outbox
- POST /superagent/outbox/flush          → Processa imediatamente mensagens pendentes na Outbox
- GET  /superagent/stats                 → Estatísticas de observabilidade do Super-Agent
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.api.auth import (
    AuthenticatedClient,
    record_audit_event,
    require_api_client,
    require_role,
)
from app.api.container import Container
from app.factory.intake import (
    DirectWorkOrderInput,
    normalize_direct_work_order,
    planner_request,
)
from app.graph.state import DeliveryConfig, WorkflowBudget
from app.models.superagent import (
    IngressEvent,
    IngressEventKind,
    OutboxMessage,
    OutboxStatus,
    RouterDecisionStatus,
    RouterTicket,
    ThreadSubscription,
)

router = APIRouter(prefix="/superagent", tags=["superagent"])


class WebhookIngressResponse(BaseModel):
    accepted: bool
    ignored: bool = False
    reason: str | None = None
    ticket: RouterTicket | None = None
    workflow_id: str | None = None


def _container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


def verify_github_webhook_signature(
    raw_body: bytes, signature_header: str | None, secret: str
) -> bool:
    if not secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=").strip()
    return hmac.compare_digest(expected, provided)


def parse_github_webhook_payload(
    event_name: str, delivery_id: str, payload: dict[str, Any]
) -> IngressEvent | None:
    """Converte payloads nativos de Webhook do GitHub em IngressEvent canônico."""
    repo_obj = payload.get("repository")
    if not isinstance(repo_obj, dict) or not isinstance(repo_obj.get("full_name"), str):
        return None
    repository = repo_obj["full_name"]
    default_branch = str(repo_obj.get("default_branch") or "main")

    sender_obj = payload.get("sender") or {}
    sender = str(sender_obj.get("login") or "unknown")
    sender_is_bot = str(sender_obj.get("type") or "").lower() == "bot"

    action = str(payload.get("action") or "")

    if event_name == "issues" and action == "opened":
        issue = payload.get("issue") or {}
        number = int(issue.get("number") or 0)
        if number <= 0:
            return None
        return IngressEvent(
            event_id=delivery_id,
            kind=IngressEventKind.ISSUE_OPENED,
            repository=repository,
            sender=sender,
            sender_is_bot=sender_is_bot,
            thread_kind="issue",
            thread_number=number,
            title=str(issue.get("title") or ""),
            body=str(issue.get("body") or ""),
            html_url=str(issue.get("html_url") or ""),
            base_ref=default_branch,
        )

    if event_name == "issue_comment" and action == "created":
        issue = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        number = int(issue.get("number") or 0)
        if number <= 0:
            return None
        is_pr = "pull_request" in issue
        return IngressEvent(
            event_id=delivery_id,
            kind=IngressEventKind.ISSUE_COMMENT,
            repository=repository,
            sender=sender,
            sender_is_bot=sender_is_bot,
            thread_kind="pull_request" if is_pr else "issue",
            thread_number=number,
            title=str(issue.get("title") or ""),
            body=str(comment.get("body") or ""),
            html_url=str(comment.get("html_url") or issue.get("html_url") or ""),
            base_ref=default_branch,
        )

    if event_name == "pull_request_review" and action == "submitted":
        pr = payload.get("pull_request") or {}
        review = payload.get("review") or {}
        number = int(pr.get("number") or 0)
        body = str(review.get("body") or "")
        if number <= 0 or not body.strip():
            return None
        base_ref = str((pr.get("base") or {}).get("ref") or default_branch)
        return IngressEvent(
            event_id=delivery_id,
            kind=IngressEventKind.PR_REVIEW,
            repository=repository,
            sender=sender,
            sender_is_bot=sender_is_bot,
            thread_kind="pull_request",
            thread_number=number,
            title=str(pr.get("title") or ""),
            body=body,
            html_url=str(review.get("html_url") or pr.get("html_url") or ""),
            base_ref=base_ref,
        )

    if event_name == "pull_request_review_comment" and action == "created":
        pr = payload.get("pull_request") or {}
        comment = payload.get("comment") or {}
        number = int(pr.get("number") or 0)
        if number <= 0:
            return None
        base_ref = str((pr.get("base") or {}).get("ref") or default_branch)
        path_info = comment.get("path")
        body = str(comment.get("body") or "")
        if isinstance(path_info, str) and path_info:
            body = f"[Arquivo: {path_info}]\n{body}"
        return IngressEvent(
            event_id=delivery_id,
            kind=IngressEventKind.PR_REVIEW_COMMENT,
            repository=repository,
            sender=sender,
            sender_is_bot=sender_is_bot,
            thread_kind="pull_request",
            thread_number=number,
            title=str(pr.get("title") or ""),
            body=body,
            html_url=str(comment.get("html_url") or pr.get("html_url") or ""),
            base_ref=base_ref,
        )

    return None


async def _dispatch_ticket_to_worker(
    container: Container,
    settings: Any,
    ticket: RouterTicket,
    *,
    owner_client_id: str,
    head_branch: str | None = None,
) -> str:
    """Cria a WorkOrder/DeliveryConfig para o Ticket, inicia o Workflow e assina a Thread."""
    project_id = ticket.repository.replace("/", "-")
    criteria = ticket.acceptance_criteria or [
        "Implementar a solução solicitada e manter os testes verdes."
    ]
    work_order = None
    budget: WorkflowBudget | None = None
    delivery: DeliveryConfig | None

    if settings.factory_mode_enabled:
        work_order = normalize_direct_work_order(
            DirectWorkOrderInput(
                repository=ticket.repository,
                base_ref=ticket.base_ref,
                scm_host=ticket.scm_host,
                requested_outcome=(
                    ticket.summary
                    if len(ticket.summary) >= 10
                    else f"Resolver demanda #{ticket.thread_number}: {ticket.summary}"
                ),
                acceptance_criteria=criteria,
                idempotency_key=f"superagent:{ticket.id}",
            )
        )
        full_request = planner_request(work_order)
        budget = WorkflowBudget(**work_order.limits.model_dump())
        delivery = DeliveryConfig(
            repository=work_order.repository.full_name,
            base_branch=work_order.repository.base_ref,
            head_branch=head_branch,
            wait_for_checks=work_order.delivery_policy.wait_for_checks,
            checks_timeout_seconds=work_order.delivery_policy.checks_timeout_seconds,
        )
    else:
        criteria_lines = "\n".join(f"- {c}" for c in criteria)
        full_request = (
            f"{ticket.summary}\n\nCritérios de aceitação do solicitante:\n{criteria_lines}"
        )
        delivery = DeliveryConfig(
            repository=ticket.repository,
            base_branch=ticket.base_ref,
            head_branch=head_branch,
            title=ticket.title or f"Forgehand: #{ticket.thread_number}",
            wait_for_checks=True,
        )

    workflow_id = await container.workflow_service.start(
        project_id=project_id,
        request=full_request,
        budget=budget,
        owner_client_id=owner_client_id,
        delivery=delivery,
        work_order=work_order,
    )
    assert container.superagent_store is not None
    container.superagent_store.update_ticket(
        ticket.id,
        status=RouterDecisionStatus.DISPATCHED,
        workflow_id=workflow_id,
    )
    container.superagent_store.subscribe_thread(
        repository=ticket.repository,
        thread_kind=ticket.thread_kind,
        thread_number=ticket.thread_number,
        workflow_id=workflow_id,
        project_id=project_id,
        owner_client_id=owner_client_id,
    )
    if (
        settings.superagent_auto_ack_comments
        and container.event_publisher is not None
    ):
        await container.event_publisher.enqueue_github_comment(
            repository=ticket.repository,
            thread_number=ticket.thread_number,
            body=(
                f"🤖 **Forgehand Super-Agent** iniciou o workflow `{workflow_id}` "
                f"para atender a esta thread (`{ticket.thread_kind} #{ticket.thread_number}`)."
            ),
            idempotency_key=f"ack:{ticket.id}:{workflow_id}",
            immediate=True,
        )
    return workflow_id


async def _handle_direct_subscription(
    container: Container,
    settings: Any,
    ticket: RouterTicket,
) -> str:
    """Processa eventos entregues via Subscription Direct (sem passar pelo LLM do Router)."""
    assert container.superagent_store is not None
    sub = container.superagent_store.get_active_subscription(
        ticket.repository, ticket.thread_kind, ticket.thread_number
    )
    assert sub is not None
    workflow_id = sub.workflow_id

    # Se o workflow atual estiver aguardando decisão no human_gate e o comentário
    # trouxer um comando de decisão explícito, retoma o próprio workflow.
    try:
        current_state = await container.workflow_service.get(workflow_id)
    except Exception:  # noqa: BLE001
        current_state = None

    normalized_text = ticket.summary.lower()
    if current_state and current_state.get("pending_decision") is not None:
        for decision_cmd in ("accept_partial", "retry", "abort"):
            if decision_cmd in normalized_text:
                await container.workflow_service.decide(workflow_id, decision_cmd)
                return workflow_id

    # Caso contrário, despacha uma execução de continuidade mantendo a mesma branch do PR
    existing_delivery = (current_state or {}).get("delivery") or {}
    existing_branch = existing_delivery.get("branch") or f"forgehand/{workflow_id[:12]}"
    continuation_ticket = ticket.model_copy(
        update={
            "summary": (
                f"Continuação da thread #{ticket.thread_number} (workflow anterior {workflow_id}). "
                f"Feedback de @{ticket.sender}: {ticket.summary}"
            )
        }
    )
    new_workflow_id = await _dispatch_ticket_to_worker(
        container,
        settings,
        continuation_ticket,
        owner_client_id=sub.owner_client_id,
        head_branch=existing_branch,
    )
    container.superagent_store.update_ticket(
        ticket.id,
        status=RouterDecisionStatus.DIRECT_SUBSCRIPTION,
        workflow_id=new_workflow_id,
    )
    return new_workflow_id


@router.post(
    "/github/webhook",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookIngressResponse,
)
async def github_webhook_ingress(request: Request) -> WebhookIngressResponse:
    settings = request.app.state.settings
    if not settings.superagent_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Super-Agent ingress não está habilitado.",
        )

    raw_body = await request.body()
    webhook_secret = os.getenv("GITHUB_WEBHOOK_SECRET", "").strip()
    signature = request.headers.get("X-Hub-Signature-256")

    if webhook_secret:
        if not verify_github_webhook_signature(raw_body, signature, webhook_secret):
            await record_audit_event(
                request,
                action="superagent_webhook_ingress",
                outcome="invalid_signature",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Assinatura X-Hub-Signature-256 inválida.",
            )
    else:
        # Sem GITHUB_WEBHOOK_SECRET configurado, exige X-API-Key com papel approver
        client = await require_api_client(request)
        require_role(client, "approver")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Corpo do webhook não é um JSON válido.",
        ) from None

    event_name = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery") or str(uuid4())
    ingress_event = parse_github_webhook_payload(event_name, delivery_id, payload)
    if ingress_event is None:
        return WebhookIngressResponse(
            accepted=False,
            ignored=True,
            reason="unsupported_github_event_or_action",
        )

    container = _container(request)
    assert container.superagent_router is not None
    assert container.superagent_store is not None
    ticket = await container.superagent_router.route(ingress_event)

    # Idempotência: se o evento já havia sido despachado anteriormente, retorna o mesmo ticket
    if (
        ticket.status == RouterDecisionStatus.DISPATCHED
        and ticket.workflow_id is not None
    ):
        return WebhookIngressResponse(
            accepted=True,
            ticket=ticket,
            workflow_id=ticket.workflow_id,
        )

    workflow_id: str | None = None
    if ticket.status == RouterDecisionStatus.DISPATCHED:
        workflow_id = await _dispatch_ticket_to_worker(
            container,
            settings,
            ticket,
            owner_client_id=settings.superagent_default_client_id,
        )
        ticket = container.superagent_store.get_ticket(ticket.id) or ticket
    elif ticket.status == RouterDecisionStatus.DIRECT_SUBSCRIPTION:
        workflow_id = await _handle_direct_subscription(container, settings, ticket)
        ticket = container.superagent_store.get_ticket(ticket.id) or ticket

    await record_audit_event(
        request,
        action="superagent_webhook_ingress",
        outcome=ticket.status.value,
        client_id=settings.superagent_default_client_id,
        project_id=ticket.repository.replace("/", "-"),
        workflow_id=workflow_id,
        status_code=status.HTTP_202_ACCEPTED,
        detail=f"event={ingress_event.kind.value};sender={ingress_event.sender};reason={ticket.reasoning}",
    )

    accepted = ticket.status in {
        RouterDecisionStatus.DISPATCHED,
        RouterDecisionStatus.DIRECT_SUBSCRIPTION,
        RouterDecisionStatus.OBSERVED,
    }
    return WebhookIngressResponse(
        accepted=accepted,
        ignored=not accepted,
        reason=ticket.reasoning,
        ticket=ticket,
        workflow_id=workflow_id,
    )


@router.get("/tickets", response_model=list[RouterTicket])
async def list_tickets(
    request: Request,
    status_filter: RouterDecisionStatus | None = Query(default=None, alias="status"),
    repository: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    client: AuthenticatedClient = Depends(require_api_client),
) -> list[RouterTicket]:
    require_role(client, "viewer")
    container = _container(request)
    assert container.superagent_store is not None
    return container.superagent_store.list_tickets(
        status=status_filter, repository=repository, limit=limit
    )


@router.post("/tickets/{ticket_id}/approve", response_model=RouterTicket)
async def approve_observed_ticket(
    ticket_id: str,
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> RouterTicket:
    require_role(client, "approver")
    container = _container(request)
    assert container.superagent_store is not None
    ticket = container.superagent_store.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Ticket não encontrado."
        )
    if ticket.status == RouterDecisionStatus.DISPATCHED and ticket.workflow_id:
        return ticket
    if ticket.status != RouterDecisionStatus.OBSERVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Apenas tickets em estado 'observed' podem ser aprovados (atual: {ticket.status.value}).",
        )

    workflow_id = await _dispatch_ticket_to_worker(
        container,
        request.app.state.settings,
        ticket,
        owner_client_id=client.client_id,
    )
    updated = container.superagent_store.get_ticket(ticket_id)
    assert updated is not None
    await record_audit_event(
        request,
        action="superagent_ticket_approve",
        outcome="dispatched",
        client_id=client.client_id,
        project_id=updated.repository.replace("/", "-"),
        workflow_id=workflow_id,
        status_code=status.HTTP_200_OK,
    )
    return updated


@router.get("/subscriptions", response_model=list[ThreadSubscription])
async def list_subscriptions(
    request: Request,
    active_only: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    client: AuthenticatedClient = Depends(require_api_client),
) -> list[ThreadSubscription]:
    require_role(client, "viewer")
    container = _container(request)
    assert container.superagent_store is not None
    return container.superagent_store.list_subscriptions(
        active_only=active_only, limit=limit
    )


@router.get("/outbox", response_model=list[OutboxMessage])
async def list_outbox(
    request: Request,
    status_filter: OutboxStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    client: AuthenticatedClient = Depends(require_api_client),
) -> list[OutboxMessage]:
    require_role(client, "viewer")
    container = _container(request)
    assert container.superagent_store is not None
    return container.superagent_store.list_outbox_messages(
        status=status_filter, limit=limit
    )


@router.post("/outbox/flush")
async def flush_outbox(
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> dict[str, int]:
    require_role(client, "operator")
    container = _container(request)
    if container.event_publisher is None:
        return {"processed": 0, "sent": 0, "failed_attempt": 0}
    return await container.event_publisher.flush_outbox()


@router.get("/stats")
async def superagent_stats(
    request: Request,
    client: AuthenticatedClient = Depends(require_api_client),
) -> dict[str, Any]:
    require_role(client, "viewer")
    container = _container(request)
    assert container.superagent_store is not None
    return container.superagent_store.stats()

