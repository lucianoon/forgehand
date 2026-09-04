"""Referências web da solicitação: extração, busca guardada e citação."""

from __future__ import annotations

import httpx
import pytest

from app.agents.grounding import (
    build_grounding_prefix,
    get_evidence_index,
    grounding_required,
    validate_citations,
)
from app.agents.planner import LLMPlanner
from app.infrastructure.memory import InMemoryProjectMemory
from app.infrastructure.settings import Settings
from app.infrastructure.web_references import (
    WebReferenceCollector,
    extract_urls,
    html_to_text,
    is_public_address,
)

PUBLIC = "93.184.216.34"


async def public_resolver(host: str) -> list[str]:
    return [PUBLIC]


async def split_resolver(host: str) -> list[str]:
    return ["10.0.0.5"] if host.startswith("internal") else [PUBLIC]


def make_collector(handler, **overrides) -> WebReferenceCollector:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    overrides.setdefault("resolver", public_resolver)
    return WebReferenceCollector(client=client, **overrides)


HTML = """<html><head><title> Guia   Oficial </title>
<script>alert('x')</script><style>p{}</style></head>
<body><h1>Instalação</h1><p>Use <b>uv sync</b> para instalar.</p>
<script>console.log('ignore me')</script><p>Segundo parágrafo.</p></body></html>"""


def test_extract_urls_dedupes_and_trims_punctuation() -> None:
    text = (
        "Veja https://docs.example.com/guia, depois (https://x.example.com/a). "
        "De novo: https://docs.example.com/guia. Sem esquema: example.com"
    )
    assert extract_urls(text) == [
        "https://docs.example.com/guia",
        "https://x.example.com/a",
    ]
    assert extract_urls(text, limit=1) == ["https://docs.example.com/guia"]
    assert extract_urls("") == []


def test_html_to_text_drops_scripts_and_keeps_title() -> None:
    text, title = html_to_text(HTML)
    assert title == "Guia Oficial"
    assert "Instalação" in text and "uv sync para instalar" in text
    assert "alert" not in text and "console.log" not in text and "p{}" not in text


def test_html_to_text_prefers_main_content_over_navigation() -> None:
    body = "<p>" + "Conteúdo principal relevante. " * 12 + "</p>"
    html = (
        "<html><body><nav><ul><li>Início</li><li>Guias</li><li>Busca</li></ul></nav>"
        f"<main><h1>Projetos</h1>{body}</main>"
        "<footer>Copyright rodapé</footer></body></html>"
    )
    text, _ = html_to_text(html)
    assert text.startswith("Projetos")
    assert "Conteúdo principal relevante" in text
    assert "Início" not in text and "rodapé" not in text

    # <main> curto demais não é confiável como recorte: cai no documento inteiro
    short = "<html><body><p>Introdução fora do main</p><main>Oi</main></body></html>"
    text, _ = html_to_text(short)
    assert "Introdução fora do main" in text and "Oi" in text

    # nav dentro de <main> (layout mkdocs) também sai
    nested = (
        "<html><body><main><nav><ul><li>Barra lateral</li></ul></nav>"
        f"<article><h1>Título</h1>{body}</article></main></body></html>"
    )
    text, _ = html_to_text(nested)
    assert text.startswith("Título") and "Barra lateral" not in text


@pytest.mark.parametrize(
    "address,public",
    [
        ("93.184.216.34", True),
        ("10.0.0.5", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("::1", False),
        ("2001:4860:4860::8888", True),
        ("not-an-ip", False),
    ],
)
def test_public_address_classification(address: str, public: bool) -> None:
    assert is_public_address(address) is public


@pytest.mark.asyncio
async def test_collect_turns_urls_into_citable_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("forgehand")
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html; charset=utf-8"})

    collector = make_collector(handler)
    references = await collector.collect(
        "Implemente a instalação conforme https://docs.example.com/guia obrigado"
    )
    assert references is not None
    [item] = references["evidence"]
    assert item["id"] == "W1" and item["status"] == "ok"
    assert item["title"] == "Guia Oficial" and item["content_type"] == "text/html"
    assert "uv sync" in item["excerpt"] and "alert" not in item["excerpt"]

    context = {"web_references": references}
    assert grounding_required(context)
    assert set(get_evidence_index(context)) == {"W1"}
    assert validate_citations(context, ["W1"]) == []
    assert validate_citations(context, ["W9"])
    prefix = build_grounding_prefix(context) or ""
    assert "[W1] https://docs.example.com/guia — Guia Oficial" in prefix
    assert "NÃO confiável" in prefix and "uv sync" in prefix
    # o planner não duplica o bloco no dump de contexto
    assert "web_references" not in LLMPlanner._non_grounding_context(context)


@pytest.mark.asyncio
async def test_collect_combines_with_repository_grounding() -> None:
    collector = make_collector(
        lambda request: httpx.Response(200, text="conteudo", headers={"content-type": "text/plain"})
    )
    references = await collector.collect("veja https://docs.example.com/x")
    context = {
        "repository_grounding": {
            "repo_root": "/repo",
            "require_citations": True,
            "evidence": [
                {"id": "E1", "path": "a.py", "line_start": 1, "line_end": 2, "excerpt": "x = 1"}
            ],
        },
        "web_references": references,
    }
    assert set(get_evidence_index(context)) == {"E1", "W1"}
    assert validate_citations(context, ["E1", "W1"]) == []
    prefix = build_grounding_prefix(context) or ""
    assert prefix.index("[E1]") < prefix.index("[W1]")


@pytest.mark.asyncio
async def test_private_hosts_are_blocked_even_via_redirect() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "public.example.com":
            return httpx.Response(302, headers={"location": "http://internal.example/secret"})
        return httpx.Response(200, text="segredo", headers={"content-type": "text/plain"})

    collector = make_collector(handler, resolver=split_resolver)
    references = await collector.collect(
        "https://public.example.com/go e também http://internal.example/direto"
    )
    assert references is not None
    by_url = {item["url"]: item for item in references["evidence"]}
    assert by_url["https://public.example.com/go"]["status"] == "error"
    assert "não público" in by_url["https://public.example.com/go"]["error"]
    assert by_url["http://internal.example/direto"]["status"] == "error"
    assert calls == ["public.example.com"]  # o host interno nunca foi contatado
    assert "não lido" in (build_grounding_prefix({"web_references": references}) or "")


@pytest.mark.asyncio
async def test_allowlist_matches_host_suffix_and_blocks_others() -> None:
    collector = make_collector(
        lambda request: httpx.Response(200, text="ok", headers={"content-type": "text/plain"}),
        allowed_hosts=["docs.example.com", ".trusted.org"],
    )
    references = await collector.collect(
        "https://docs.example.com/a https://api.docs.example.com/b "
        "https://wiki.trusted.org/c https://evil.example.com/d"
    )
    assert references is not None
    statuses = {item["url"].split("//")[1].split("/")[0]: item["status"] for item in references["evidence"]}
    assert statuses == {
        "docs.example.com": "ok",
        "api.docs.example.com": "ok",
        "wiki.trusted.org": "ok",
        "evil.example.com": "error",
    }


@pytest.mark.asyncio
async def test_scheme_port_credentials_and_content_type_guards() -> None:
    collector = make_collector(
        lambda request: httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}),
    )
    pdf = await collector.fetch("https://docs.example.com/manual.pdf")
    assert pdf["status"] == "error" and "não é textual" in pdf["error"]

    port = await collector.fetch("https://docs.example.com:8443/x")
    assert port["status"] == "error" and "porta" in port["error"]

    creds = await collector.fetch("https://user:pw@docs.example.com/x")
    assert creds["status"] == "error" and "credenciais" in creds["error"]


@pytest.mark.asyncio
async def test_size_limits_truncate_and_flag() -> None:
    collector = make_collector(
        lambda request: httpx.Response(200, text="a" * 5000, headers={"content-type": "text/plain"}),
        max_chars=500,
    )
    item = await collector.fetch("https://docs.example.com/big")
    assert item["status"] == "ok" and item["truncated"] is True
    assert item["excerpt"].startswith("a" * 500) and "truncado" in item["excerpt"]


@pytest.mark.asyncio
async def test_max_urls_lists_omitted_instead_of_fetching() -> None:
    collector = make_collector(
        lambda request: httpx.Response(200, text="ok", headers={"content-type": "text/plain"}),
        max_urls=1,
    )
    references = await collector.collect("https://a.example.com/1 https://b.example.com/2")
    assert references is not None
    assert [item["id"] for item in references["evidence"]] == ["W1"]
    assert references["omitted_urls"] == ["https://b.example.com/2"]
    assert "não foram buscadas" in (build_grounding_prefix({"web_references": references}) or "")


@pytest.mark.asyncio
async def test_collect_returns_none_without_urls() -> None:
    collector = make_collector(lambda request: httpx.Response(500))
    assert await collector.collect("nenhum link aqui") is None


class _FakeCollector:
    def __init__(self) -> None:
        self.requests: list[str] = []

    async def collect(self, request: str):
        self.requests.append(request)
        return {
            "source": "web_references",
            "require_citations": True,
            "omitted_urls": [],
            "evidence": [{"id": "W1", "url": "https://x", "status": "ok", "excerpt": "e"}],
        }


@pytest.mark.asyncio
async def test_memory_injects_web_references_only_when_enabled() -> None:
    disabled = InMemoryProjectMemory(
        Settings(_env_file=None, repository_grounding_enabled=False)
    )
    assert disabled._web_collector is None
    assert "web_references" not in await disabled.load_context("p", "veja https://x")

    enabled = InMemoryProjectMemory(
        Settings(
            _env_file=None,
            repository_grounding_enabled=False,
            web_references_enabled=True,
            web_references_allowed_hosts="docs.example.com, .trusted.org",
        )
    )
    assert enabled._web_collector is not None
    assert enabled._web_collector.allowed_hosts == ["docs.example.com", ".trusted.org"]
    fake = _FakeCollector()
    enabled._web_collector = fake  # type: ignore[assignment]
    context = await enabled.load_context("p", "veja https://x")
    assert context["web_references"]["evidence"][0]["id"] == "W1"
    assert fake.requests == ["veja https://x"]
    # o caminho da factory (load_project_context) também recebe as referências
    assert "web_references" in await enabled.load_project_context("p", "veja https://x")
