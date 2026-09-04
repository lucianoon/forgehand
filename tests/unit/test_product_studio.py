import asyncio
import io
import json
import zipfile
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.agents.product import ProductStudio, demo_archive, demo_document
from app.infrastructure.product_store import ProductConflict, ProductStore
from app.models.product import DemoApp, ProductBrief, ProductIdea
from app.providers.base import CompletionResult, Usage

BRIEF = ProductBrief(name="Agenda", audience="Barbearias", outcome="Organizar agendamentos",
                     features=["Cadastrar agendamento"], backlog=["Criar cadastro", "Validar com recepção"],
                     acceptance_criteria=["Adicionar e editar um agendamento"], out_of_scope=["Sem backend ou login"])
DEMO = DemoApp.model_validate({
    "name": "Agenda", "description": "Atendimentos do dia", "theme": "forest",
    "entities": [{"id": "appointments", "name": "Agendamentos", "fields": [
        {"id": "client", "label": "Cliente", "kind": "text", "required": True, "options": []},
        {"id": "date", "label": "Data", "kind": "date", "required": True, "options": []}],
        "records": [{"values": ["Ana (exemplo)", "2026-09-10"]}]}]})


class Router:
    def __init__(self):
        self.calls = []
        self.fail = False

    def estimate_request_cost(self, tier, request):
        return 0.02

    async def complete(self, tier, request):
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("sensitive provider payload must not persist")
        value = BRIEF if request.response_schema is ProductBrief else DEMO
        return CompletionResult(text="", parsed=value.model_dump(), model="test", provider="test",
                                usage=Usage(input_tokens=10, output_tokens=10), cost_usd=0.001, latency_ms=1)


def idea(**updates):
    return ProductIdea(project_id="p", idea="Organizar os agendamentos da barbearia", audience="Recepcionistas",
                       idempotency_key="test-key-1", **updates)


@pytest.mark.asyncio
async def test_approval_revised_scope_restart_replay_and_archive(tmp_path):
    router = Router()
    service = ProductStudio(router, ProductStore(str(tmp_path / "db")))
    first = await service.create("owner", idea())
    assert first["status"] == "approval_required" and first["app"] is None
    assert (await service.create("owner", idea()))["id"] == first["id"]
    assert len(router.calls) == 1
    changed = BRIEF.model_copy(update={"name": "Agenda revisada"})
    built = await service.approve("owner", first["id"], changed)
    assert built["status"] == "ready_for_preview"
    assert "Agenda revisada" in router.calls[-1].messages[0].content
    assert built["brief"]["name"] == "Agenda revisada"
    assert (await service.approve("owner", first["id"], changed)) == built
    assert len(router.calls) == 2 and built["cost_usd"] == pytest.approx(.002)
    assert built["reserved_usd"] == pytest.approx(0)
    restored = ProductStore(str(tmp_path / "db"))
    assert restored.get(first["id"], "owner") == built
    with pytest.raises(KeyError):
        restored.get(first["id"], "other")
    assert restored.list("owner", "other-project") == []
    with zipfile.ZipFile(io.BytesIO(demo_archive(built))) as archive:
        assert set(archive.namelist()) == {"index.html", "model.json", "brief.json", "README.md"}
        assert "Agenda revisada" in archive.read("brief.json").decode()
        assert "Adicionar registro" in archive.read("index.html").decode()


@pytest.mark.asyncio
async def test_budget_failure_reservation_and_idempotency_conflict(tmp_path):
    router = Router()
    service = ProductStudio(router, ProductStore(str(tmp_path / "db")))
    record = await service.create("low-budget", idea(max_cost_usd=.01))
    assert record["error"] == "insufficient_budget" and not router.calls
    router.fail = True
    failed = await service.create("owner", idea())
    assert failed["status"] == "failed" and failed["reserved_usd"] == .02
    assert "sensitive" not in json.dumps(failed)
    with pytest.raises(ProductConflict):
        await service.create("owner", idea().model_copy(update={"idea": "Outra ideia completamente diferente"}))
    with pytest.raises(ProductConflict):
        await service.approve("owner", failed["id"], BRIEF)


@pytest.mark.asyncio
async def test_duplicate_approval_does_not_generate_twice(tmp_path):
    router = Router()
    service = ProductStudio(router, ProductStore(str(tmp_path / "db")))
    product = await service.create("owner", idea())
    start, finish = asyncio.Event(), asyncio.Event()
    original = router.complete

    async def slow(tier, request):
        start.set()
        await finish.wait()
        return await original(tier, request)

    router.complete = slow
    task = asyncio.create_task(service.approve("owner", product["id"], BRIEF))
    await start.wait()
    duplicate = await service.approve("owner", product["id"], BRIEF)
    assert duplicate["status"] == "building"
    with pytest.raises(ProductConflict):
        await service.approve("owner", product["id"], BRIEF.model_copy(update={"name": "outro"}))
    finish.set()
    await task
    assert len(router.calls) == 2


@pytest.mark.asyncio
async def test_abandoned_operation_expires_without_model_retry(tmp_path):
    store = ProductStore(str(tmp_path / "db"))
    product, _ = store.create("owner", idea().model_dump(), "hash")
    with store.connection() as db:
        db.execute("UPDATE products SET updated=0 WHERE id=?", (product["id"],))
    assert store.get(product["id"], "owner")["error"] == "operation_interrupted"
    assert not store.transition({**product, "status": "approval_required"}, "drafting")


def test_model_content_cannot_become_executable_markup():
    hostile = DEMO.model_copy(update={"name": "</script><script>parent.document.body.remove()</script>"})
    document = demo_document(hostile)
    assert "\\u003c/script>" in document
    assert "<script>parent.document" not in document
    assert "connect-src 'none'" in document and "form-action 'none'" in document
    assert "textContent" in document
    bad = DEMO.model_dump()
    bad["entities"][0]["fields"].append(bad["entities"][0]["fields"][0])
    with pytest.raises(ValidationError):
        DemoApp.model_validate(bad)


@pytest.mark.asyncio
async def test_authenticated_api_approval_and_download(tmp_path):
    from app.main import create_app
    from app.infrastructure.audit import InMemoryAuditLog
    from app.infrastructure.settings import Settings

    app = create_app()
    app.state.settings = Settings(api_keys_json=json.dumps({
        "owner-key": {"client_id": "owner", "role": "admin", "projects": ["p"]},
        "other-key": {"client_id": "other", "role": "admin", "projects": ["p"]},
        "viewer-key": {"client_id": "owner", "role": "viewer", "projects": ["p"]},
    }))
    app.state.container = SimpleNamespace(
        product_studio=ProductStudio(Router(), ProductStore(str(tmp_path / "db"))),
        audit_log=InMemoryAuditLog())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/products", json=idea().model_dump())).status_code == 401
        headers = {"X-API-Key": "owner-key"}
        created = await client.post("/products", json=idea().model_dump(), headers=headers)
        assert created.status_code == 201
        path = "/products/" + created.json()["id"]
        assert (await client.get(path, headers={"X-API-Key": "other-key"})).status_code == 404
        assert (await client.get(path + "/download", headers=headers)).status_code == 409
        assert (await client.get(path + "/fullstack", headers=headers)).status_code == 409
        assert (await client.post(path + "/approve", json={"brief": BRIEF.model_dump()},
                                  headers={"X-API-Key": "viewer-key"})).status_code == 403
        built = await client.post(path + "/approve", json={"brief": BRIEF.model_dump()}, headers=headers)
        assert built.json()["status"] == "ready_for_preview"
        preview = await client.get(path + "/preview", headers=headers)
        assert preview.headers["content-type"].startswith("application/json")
        assert "<!doctype html>" in preview.json()["document"]
        downloaded = await client.get(path + "/download", headers=headers)
        assert downloaded.headers["content-type"] == "application/zip"
        fullstack = await client.get(path + "/fullstack", headers=headers)
        assert fullstack.status_code == 200
        with zipfile.ZipFile(io.BytesIO(fullstack.content)) as archive:
            assert "runtime/server.py" in archive.namelist()
            assert "manifest.json" in archive.namelist()
        assert len(app.state.container.product_studio.router.calls) == 2
        assert (await client.get(path + "/fullstack", headers={"X-API-Key":"other-key"})).status_code == 404
        app.state.container.product_studio = None
        assert (await client.get("/products?project_id=p", headers=headers)).status_code == 503
