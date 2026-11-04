"""Testes unitários e de integração da camada Super-Agent:
- Filtragem em 2 níveis (Fast Path programático + Slow Path semântico via LLM leve)
- Modos proativos (off, observe, auto)
- Subscription Direct (bypass do Router para Threads já assinadas)
- Fila Outbox persistente com idempotência e retry
- Ingress Webhook do GitHub com assinatura HMAC-SHA256 e aprovação de tickets
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import AsyncMock

import anthropic
import httpx
import pytest
from fastapi import FastAPI
from langgraph.checkpoint.memory import MemorySaver

from app.agents.superagent_router import SuperAgentRouter
from app.api.container import build_container
from app.api.routes.operations import router as operations_router
from app.api.routes.superagent import router as superagent_router
from app.api.routes.workflows import router as workflows_router
from app.graph.workflow import build_serde
from app.infrastructure.scm import GitHubSCMClient, StaticTokenProvider
from app.infrastructure.settings import Settings
from app.infrastructure.superagent_store import SuperAgentStore
from app.infrastructure.webhooks import WebhookDispatcher
from app.infrastructure.workflow_queue import InMemoryWorkflowQueue
from app.models.superagent import (
    IngressEvent,
    IngressEventKind,
    OutboxStatus,
    ProactiveMode,
    RouterDecisionStatus,
)
from app.providers.base import CompletionResult, Usage


def _sample_issue_event(
    *,
    event_id: str = "evt-1",
    sender: str = "alice",
    sender_is_bot: bool = False,
    repository: str = "acme/widgets",
    thread_number: int = 42,
    title: str = "Erro 500 ao salvar cliente sem telefone",
    body: str = "Quando o campo phone vai nulo o endpoint falha com KeyError.",
) -> IngressEvent:
    return IngressEvent(
        event_id=event_id,
        kind=IngressEventKind.ISSUE_OPENED,
        repository=repository,
        sender=sender,
        sender_is_bot=sender_is_bot,
        thread_kind="issue",
        thread_number=thread_number,
        title=title,
        body=body,
        html_url=f"https://github.com/{repository}/issues/{thread_number}",
    )


@pytest.mark.asyncio
async def test_router_fast_path_blocks_bots_and_unauthorized_senders() -> None:
    store = SuperAgentStore(":memory:")
    mock_provider = AsyncMock()
    router = SuperAgentRouter(
        store,
        provider_router=mock_provider,
        proactive_mode=ProactiveMode.AUTO,
        allowed_senders=["alice"],
        approved_repositories=["acme/widgets"],
    )

    # 1. Bot é bloqueado no Fast Path sem chamar o LLM
    bot_ticket = await router.route(
        _sample_issue_event(event_id="evt-bot", sender="dependabot[bot]", sender_is_bot=True)
    )
    assert bot_ticket.status == RouterDecisionStatus.REJECTED_FAST_PATH
    assert bot_ticket.reasoning == "sender_is_bot"

    # 2. Usuário fora da allowlist é bloqueado no Fast Path
    unauth_ticket = await router.route(
        _sample_issue_event(event_id="evt-mallory", sender="mallory")
    )
    assert unauth_ticket.status == RouterDecisionStatus.REJECTED_FAST_PATH
    assert unauth_ticket.reasoning == "sender_not_in_allowlist"

    # 3. Repositório não aprovado é bloqueado no Fast Path
    repo_ticket = await router.route(
        _sample_issue_event(
            event_id="evt-other-repo",
            sender="alice",
            repository="other/repo",
        )
    )
    assert repo_ticket.status == RouterDecisionStatus.REJECTED_FAST_PATH
    assert repo_ticket.reasoning == "repository_not_approved"
    assert mock_provider.complete.await_count == 0


@pytest.mark.asyncio
async def test_router_proactive_modes_off_observe_and_auto() -> None:
    store = SuperAgentStore(":memory:")
    mock_provider = AsyncMock()
    mock_provider.complete.return_value = CompletionResult(
        text="",
        parsed={
            "should_act": True,
            "confidence": 0.92,
            "summary": "Corrigir tratamento de phone nulo no endpoint de clientes",
            "acceptance_criteria": [
                "Aceitar phone nulo sem KeyError",
                "Adicionar teste unitário para cliente sem telefone",
            ],
            "reasoning": "Bug claro reportado com causa identificada.",
        },
        model="claude-haiku-4-5",
        provider="anthropic",
        usage=Usage(input_tokens=120, output_tokens=60),
        cost_usd=0.0004,
        latency_ms=15.0,
    )

    # Modo OFF: sem @forgehand é descartado no Fast Path (0 chamadas de LLM)
    router_off = SuperAgentRouter(
        store,
        provider_router=mock_provider,
        proactive_mode=ProactiveMode.OFF,
    )
    off_ticket = await router_off.route(_sample_issue_event(event_id="evt-off"))
    assert off_ticket.status == RouterDecisionStatus.REJECTED_FAST_PATH
    assert off_ticket.reasoning == "proactive_mode_off_without_mention"
    assert mock_provider.complete.await_count == 0

    # Modo OFF com @forgehand explícito: passa para o Slow Path e despacha!
    mention_ticket = await router_off.route(
        _sample_issue_event(
            event_id="evt-mention",
            body="@forgehand por favor corrija esse erro 500 quando phone for nulo.",
        )
    )
    assert mention_ticket.status == RouterDecisionStatus.DISPATCHED
    assert mock_provider.complete.await_count == 1

    # Modo OBSERVE sem @forgehand: registra como OBSERVED ("teria aceitado") sem abrir workflow
    router_observe = SuperAgentRouter(
        store,
        provider_router=mock_provider,
        proactive_mode=ProactiveMode.OBSERVE,
    )
    obs_ticket = await router_observe.route(_sample_issue_event(event_id="evt-observe"))
    assert obs_ticket.status == RouterDecisionStatus.OBSERVED
    assert obs_ticket.confidence == pytest.approx(0.92)
    assert len(obs_ticket.acceptance_criteria) == 2

    # Modo AUTO sem @forgehand: despacha automaticamente
    router_auto = SuperAgentRouter(
        store,
        provider_router=mock_provider,
        proactive_mode=ProactiveMode.AUTO,
    )
    auto_ticket = await router_auto.route(_sample_issue_event(event_id="evt-auto"))
    assert auto_ticket.status == RouterDecisionStatus.DISPATCHED


@pytest.mark.asyncio
async def test_subscription_direct_bypasses_router_llm() -> None:
    store = SuperAgentStore(":memory:")
    store.subscribe_thread(
        repository="acme/widgets",
        thread_kind="pull_request",
        thread_number=99,
        workflow_id="wf-existing-99",
        project_id="acme-widgets",
        owner_client_id="dev-client",
    )
    mock_provider = AsyncMock()
    router = SuperAgentRouter(
        store,
        provider_router=mock_provider,
        proactive_mode=ProactiveMode.OFF,
    )

    # Comentário sem @forgehand em uma Thread já assinada vai direto via Subscription Direct
    event = IngressEvent(
        event_id="evt-pr-comment-1",
        kind=IngressEventKind.PR_REVIEW_COMMENT,
        repository="acme/widgets",
        sender="alice",
        thread_kind="pull_request",
        thread_number=99,
        title="Fix endpoint",
        body="Renomeie a variável `tmp` para `normalized_phone` e rode os testes.",
    )
    ticket = await router.route(event)

    assert ticket.status == RouterDecisionStatus.DIRECT_SUBSCRIPTION
    assert ticket.workflow_id == "wf-existing-99"
    assert ticket.reasoning == "subscription_direct_bypass"
    # Nenhuma chamada ao LLM do Router foi feita!
    assert mock_provider.complete.await_count == 0

    stats = store.stats()
    assert stats["subscriptions"]["active_threads"] == 1
    assert stats["subscriptions"]["direct_deliveries_total"] == 1


@pytest.mark.asyncio
async def test_outbox_persistence_idempotency_and_retry(tmp_path: Path) -> None:
    store = SuperAgentStore(tmp_path / "superagent.sqlite3")
    attempts = {"webhook": 0, "github": 0}

    def webhook_handler(request: httpx.Request) -> httpx.Response:
        attempts["webhook"] += 1
        if attempts["webhook"] == 1:
            return httpx.Response(503, text="temporary outage")
        return httpx.Response(202)

    def github_handler(request: httpx.Request) -> httpx.Response:
        attempts["github"] += 1
        assert request.url.path == "/repos/acme/widgets/issues/42/comments"
        return httpx.Response(201, json={"id": 1001, "body": "ok"})

    def scm_factory() -> GitHubSCMClient:
        return GitHubSCMClient(
            token_provider=StaticTokenProvider("ghs_test"),
            client=httpx.AsyncClient(
                base_url="https://api.github.com",
                transport=httpx.MockTransport(github_handler),
            ),
        )

    dispatcher = WebhookDispatcher(
        ["https://hooks.test/events"],
        "signing-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(webhook_handler)),
        store=store,
        scm_client_factory=scm_factory,
        max_attempts=3,
        base_backoff_seconds=10.0,
    )

    # 1ª tentativa do webhook falha com 503 → fica PENDING na Outbox com backoff
    await dispatcher.publish(
        "workflow.processed",
        {"workflow_id": "wf-1"},
        idempotency_key="wf-1-done",
    )
    # Chamada duplicada com mesma idempotency_key não duplica registro na Outbox
    await dispatcher.publish(
        "workflow.processed",
        {"workflow_id": "wf-1"},
        idempotency_key="wf-1-done",
    )
    pending = store.list_outbox_messages(status=OutboxStatus.PENDING)
    assert len(pending) == 1
    assert pending[0].attempt_count == 1
    assert "503" in (pending[0].last_error or "")

    # Enfileira comentário no GitHub via Outbox (sucesso imediato)
    gh_msg = await dispatcher.enqueue_github_comment(
        repository="acme/widgets",
        thread_number=42,
        body="Workflow iniciado!",
        idempotency_key="ack-42",
    )
    assert gh_msg.status == OutboxStatus.SENT
    assert attempts["github"] == 1

    # Flush avançando o relógio supera o backoff e conclui o envio do webhook
    flush_stats = await dispatcher.flush_outbox(now=pending[0].next_retry_at + 1.0)
    assert flush_stats["sent"] == 1
    sent_all = store.list_outbox_messages(status=OutboxStatus.SENT)
    assert len(sent_all) == 2


def _mock_llm_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    props = body["tools"][0]["input_schema"].get("properties", {})
    model = body["model"]

    def _emit(payload: dict) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "emit_structured_output",
                        "input": payload,
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )

    if "rationale" in props:
        return _emit(
            {
                "rationale": "plano simples",
                "tasks": [
                    {
                        "title": "docs",
                        "description": "Atualizar README",
                        "capability": "documentation",
                        "acceptance_criteria": ["README atualizado"],
                        "depends_on": [],
                        "is_critical": False,
                    }
                ],
            }
        )
    if "operations" in props:
        return _emit(
            {
                "summary": "README atualizado",
                "operations": [
                    {"op": "create", "path": "README.md", "content": "# Ok\n"}
                ],
                "notes": [],
            }
        )
    return _emit(
        {
            "criteria": [
                {
                    "criterion": "README atualizado",
                    "score": 1.0,
                    "reasoning": "ok",
                }
            ],
            "failures": [],
            "required_changes": [],
            "overall_score": 1.0,
            "approved": True,
        }
    )


@pytest.mark.asyncio
async def test_superagent_webhook_ingress_observe_approve_and_direct_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "top-secret-webhook")
    settings = Settings(
        superagent_enabled=True,
        superagent_proactive_mode="observe",
        superagent_allowed_senders_json='["alice"]',
        superagent_approved_repositories_json='["acme/widgets"]',
    )
    mock_anthropic = anthropic.AsyncAnthropic(
        api_key="test",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(_mock_llm_handler)
        ),
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.container = build_container(
        settings,
        MemorySaver(serde=build_serde()),
        InMemoryWorkflowQueue(),
        run_workers=False,
        anthropic_client=mock_anthropic,
    )
    app.include_router(operations_router)
    app.include_router(workflows_router)
    app.include_router(superagent_router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. Webhook de Issue aberta sem @forgehand em modo OBSERVE → cria ticket OBSERVED
        issue_payload = {
            "action": "opened",
            "repository": {"full_name": "acme/widgets", "default_branch": "main"},
            "sender": {"login": "alice", "type": "User"},
            "issue": {
                "number": 15,
                "title": "Documentar endpoint de healthcheck no README",
                "body": "Precisamos documentar exemplos de uso do /health e /readyz no README.",
                "html_url": "https://github.com/acme/widgets/issues/15",
            },
        }
        raw = json.dumps(issue_payload).encode()
        sig = hmac.new(b"top-secret-webhook", raw, hashlib.sha256).hexdigest()

        res = await client.post(
            "/superagent/github/webhook",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "delivery-15",
                "X-Hub-Signature-256": f"sha256={sig}",
            },
        )
        assert res.status_code == 202
        body = res.json()
        assert body["accepted"] is True
        assert body["ticket"]["status"] == "observed"
        assert body["workflow_id"] is None
        ticket_id = body["ticket"]["id"]

        # 2. Operador lista candidatos proativos e aprova o ticket com 1 chamada
        listed = await client.get(
            "/superagent/tickets?status=observed",
            headers={"X-API-Key": "dev-key"},
        )
        assert listed.status_code == 200
        assert len(listed.json()) == 1

        approved = await client.post(
            f"/superagent/tickets/{ticket_id}/approve",
            headers={"X-API-Key": "dev-key"},
        )
        assert approved.status_code == 200
        approved_ticket = approved.json()
        assert approved_ticket["status"] == "dispatched"
        assert approved_ticket["workflow_id"] is not None

        # 3. Verifica que a Thread #15 agora possui assinatura ativa (Subscription Direct)
        subs = await client.get(
            "/superagent/subscriptions",
            headers={"X-API-Key": "dev-key"},
        )
        assert subs.status_code == 200
        assert len(subs.json()) == 1
        assert subs.json()[0]["thread_number"] == 15

        # 4. Novo comentário na mesma Issue #15 aciona Subscription Direct automaticamente
        comment_payload = {
            "action": "created",
            "repository": {"full_name": "acme/widgets", "default_branch": "main"},
            "sender": {"login": "alice", "type": "User"},
            "issue": {
                "number": 15,
                "title": "Documentar endpoint de healthcheck no README",
                "html_url": "https://github.com/acme/widgets/issues/15",
            },
            "comment": {
                "body": "Inclua também um exemplo de resposta JSON para o /readyz.",
                "html_url": "https://github.com/acme/widgets/issues/15#issuecomment-1",
            },
        }
        raw_comment = json.dumps(comment_payload).encode()
        sig_comment = hmac.new(
            b"top-secret-webhook", raw_comment, hashlib.sha256
        ).hexdigest()

        res_direct = await client.post(
            "/superagent/github/webhook",
            content=raw_comment,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": "delivery-15-comment-1",
                "X-Hub-Signature-256": f"sha256={sig_comment}",
            },
        )
        assert res_direct.status_code == 202
        direct_body = res_direct.json()
        assert direct_body["accepted"] is True
        assert direct_body["ticket"]["status"] == "direct_subscription"
        assert direct_body["workflow_id"] is not None

        # 5. Verifica métricas consolidadas em /superagent/stats e /metrics
        stats_res = await client.get(
            "/superagent/stats",
            headers={"X-API-Key": "dev-key"},
        )
        assert stats_res.status_code == 200
        stats = stats_res.json()
        assert stats["tickets"]["dispatched"] == 1
        assert stats["tickets"]["direct_subscription"] == 1
        assert stats["subscriptions"]["direct_deliveries_total"] == 1

