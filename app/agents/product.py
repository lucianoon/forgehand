"""Idea -> approved brief -> declarative, downloadable browser demo."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
from typing import Any
import zipfile

from app.infrastructure.product_store import ProductConflict, ProductStore
from app.infrastructure.product_delivery_store import ProductDeliveryStore
from app.models.product import DemoApp, ProductBrief, ProductIdea
from app.providers.base import CompletionRequest, Message
from app.providers.registry import ModelTier, ProviderRouter

BRIEF_PROMPT = """Você é o analista de produto do Forgehand. Converta a ideia em
um MVP de gestão de registros demonstrável no navegador. Suporte: entidades com
campos texto/número/data/hora/opções, cadastro, edição pelo formulário, exclusão com
confirmação e busca textual única em todos os campos. Lista em cartões, não tabela.
Exportação somente JSON com todos os registros de todas as entidades (não CSV,
não apenas filtrados), disponível no pacote baixado. Dados apenas nesta sessão.
No máximo quatro entidades. Não prometa algoritmos ou funcionalidades fora desse
motor. Backlog ordenado e critérios manualmente verificáveis. Inclua nos limites:
sem backend, login real, pagamentos, integração externa ou persistência multiusuário.
Texto em português. Não declare trabalho realizado; descreva o que será construído."""
APP_PROMPT = """Modele uma aplicação de gestão de registros conforme o briefing
aprovado. Gere somente dados no schema, nunca HTML/JavaScript. Campos suportados:
text, number, date (YYYY-MM-DD), time (HH:MM), select (options obrigatórias).
Crie até quatro entidades, com campos úteis para o público e dados fictícios de
exemplo; cada registro tem values na mesma ordem dos fields. Não use credenciais,
dados pessoais reais ou integrações externas. O renderer já oferece CRUD, busca
e exportação JSON. Nomes em português; IDs estáveis ascii."""


class ProductStudio:
    def __init__(self, router: ProviderRouter, store: ProductStore):
        self.router, self.store = router, store
        self.deliveries = ProductDeliveryStore(store)

    async def create(self, owner: str, idea: ProductIdea) -> dict[str, Any]:
        values = idea.model_dump()
        fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        product, fresh = self.store.create(owner, values, fingerprint)
        if fresh:
            await self._generate(product, ProductBrief, BRIEF_PROMPT,
                                 json.dumps({"idea": idea.idea, "audience": idea.audience}), "brief", 2200)
        return self.store.get(product["id"], owner)

    async def approve(self, owner: str, product_id: str, brief: ProductBrief) -> dict[str, Any]:
        product = self.store.get(product_id, owner)
        if product["status"] in {"building", "ready_for_preview"}:
            if product["brief"] != brief.model_dump():
                raise ProductConflict("Aprovação já registrada com outro escopo.")
            return product
        if product["status"] != "approval_required":
            raise ProductConflict("Projeto não está aguardando aprovação.")
        product.update(brief=brief.model_dump(), status="building")
        if not self.store.transition(product, "approval_required"):
            raise ProductConflict("Outra aprovação está em andamento.")
        await self._generate(product, DemoApp, APP_PROMPT, brief.model_dump_json(), "app", 5000)
        return self.store.get(product_id, owner)

    async def _generate(self, product: dict[str, Any], schema: Any, prompt: str,
                        content: str, target: str, max_tokens: int) -> None:
        state = product["status"]
        request = CompletionRequest(model="", system=prompt, messages=[Message(role="user", content=content)],
                                    response_schema=schema, max_tokens=max_tokens, timeout_seconds=45)
        try:
            reserve = self.router.estimate_request_cost(ModelTier.STANDARD, request)
            remaining = product["max_cost_usd"] - product["cost_usd"] - product["reserved_usd"]
            if reserve > remaining:
                product.update(status="failed", error="insufficient_budget")
                self.store.transition(product, state)
                return
            product["reserved_usd"] += reserve
            if not self.store.transition(product, state):
                raise ProductConflict("Operação não está mais ativa.")
            async with asyncio.timeout(210):
                result = await self.router.complete(ModelTier.STANDARD, request)
            product["cost_usd"] += result.cost_usd
            product["tokens"] += result.usage.total_tokens
            product["reserved_usd"] -= reserve
            product[target] = result.parse_as(schema).model_dump()
            product["status"] = "approval_required" if target == "brief" else "ready_for_preview"
            if product["cost_usd"] > product["max_cost_usd"]:
                product.update(status="failed", error="budget_exceeded")
        except asyncio.CancelledError:
            product.update(status="failed", error="operation_interrupted")
            self.store.transition(product, state)
            raise
        except Exception as exc:
            # Never persist raw provider messages or chained response bodies.
            product.update(status="failed", error=type(exc).__name__)
        self.store.transition(product, state)


def demo_document(app: DemoApp) -> str:
    assets = Path(__file__).resolve().parents[1] / "web"
    config = app.model_dump_json().replace("<", "\\u003c").replace("&", "\\u0026")
    css = (assets / "demo.css").read_text()
    script = (assets / "demo.js").read_text()
    return ('<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            "script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
            "connect-src 'none'; form-action 'none'; base-uri 'none'\">"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Demo · Forgehand</title><style>{css}</style></head><body>"
            '<main id="demo"></main><script id="model" type="application/json">'
            f"{config}</script><script>{script}</script></body></html>")


def demo_archive(product: dict[str, Any]) -> bytes:
    app = DemoApp.model_validate(product["app"])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", demo_document(app))
        archive.writestr("model.json", app.model_dump_json(indent=2))
        archive.writestr("brief.json", json.dumps(product["brief"], indent=2, ensure_ascii=False))
        archive.writestr("README.md", "# Sua primeira versão\n\nAbra index.html no navegador. Sem instalação ou serviços externos.\n\n"
                         "Demo frontend: cadastro, edição, exclusão, busca e exportação JSON. "
                         "Os dados vivem nesta sessão; exporte antes de fechar. Sem backend, login real ou banco compartilhado.\n\n"
                         "Valide manualmente os critérios em brief.json. A geração não certifica correção funcional ou prontidão para produção.\n"
                         "O código e o modelo utilizados estão embutidos em index.html. model.json é uma cópia para referência; "
                         "editá-lo isoladamente não altera a aplicação. Para personalizar, edite também o JSON no script de id model em index.html.\n")
    return buffer.getvalue()
