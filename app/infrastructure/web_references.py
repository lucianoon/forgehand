"""Referências web fornecidas na solicitação → evidência citável.

Primeira peça do acesso à web: URLs presentes no texto do pedido são buscadas
UMA vez pelo controlador, convertidas em texto e injetadas no contexto como
evidências [W1], [W2]..., no mesmo circuito de citações do grounding do
repositório. O sandbox continua sem rede; quem busca é o controlador, com as
guardas abaixo:

- só http/https em 80/443 (porta explícita apenas para host da allowlist);
- o host é resolvido ANTES da conexão e endereços privados, loopback,
  link-local, reservados ou multicast são recusados (SSRF), inclusive em cada
  salto de redirecionamento;
- allowlist opcional por sufixo de host (WEB_REFERENCES_ALLOWED_HOSTS);
- limite de bytes lidos, de caracteres no prompt e timeout curto;
- só content-type textual; HTML vira texto sem script/style.

O conteúdo baixado é DADO, não instrução: entra no bloco de evidências com
esse aviso explícito e nunca no system prompt. Falha de busca vira um item
com status="error", para o agente saber que o link não pôde ser lido.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import certifi
import httpx

if TYPE_CHECKING:  # só anotação: settings importa metade do app, evitar ciclo
    from app.infrastructure.settings import Settings

_URL_PATTERN = re.compile(r"https?://[^\s<>\"'`\]\)\}]+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?"
_TEXT_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/ld+json",
)
_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_MAX_REDIRECTS = 3
_USER_AGENT = "forgehand-web-references/1"
_TRUNCATION_MARKER = "\n[... conteúdo truncado ...]"

Resolver = Callable[[str], Awaitable[list[str]]]


class WebReferenceBlocked(ValueError):
    """URL recusada por política (esquema, porta, host, endereço, tipo)."""


def extract_urls(text: str, *, limit: int | None = None) -> list[str]:
    """URLs http(s) do texto, sem duplicatas, na ordem em que aparecem."""
    urls: list[str] = []
    for match in _URL_PATTERN.finditer(text or ""):
        url = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        if url and url not in urls:
            urls.append(url)
    return urls if limit is None else urls[:limit]


class _TextExtractor(HTMLParser):
    # nav/footer: menus e rodapés são navegação por definição — em geradores
    # como mkdocs a barra lateral vive DENTRO de <main>, então só preferir o
    # conteúdo principal não bastaria.
    _SKIP = {"script", "style", "noscript", "template", "svg", "head", "nav", "footer"}
    _BLOCK = {
        "p", "div", "br", "li", "tr", "td", "th", "ul", "ol", "table",
        "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
        "footer", "pre", "blockquote", "nav", "main", "aside",
    }

    # Conteúdo principal: quando a página marca <main>/<article>, o texto de
    # fora (menus, rodapé, busca) é ruído que só consome tokens.
    _MAIN = {"main", "article"}
    _MIN_MAIN_CHARS = 200

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._main_depth = 0
        self._in_title = False
        self._parts: list[str] = []
        self._main_parts: list[str] = []
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag in self._MAIN:
            self._main_depth += 1
        if tag in self._BLOCK:
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag in self._BLOCK:
            self._emit("\n")
        if tag in self._MAIN and self._main_depth:
            self._main_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self._emit(data)

    def _emit(self, fragment: str) -> None:
        self._parts.append(fragment)
        if self._main_depth:
            self._main_parts.append(fragment)

    def text(self) -> str:
        main_text = "".join(self._main_parts)
        source = (
            main_text
            if len(main_text.strip()) >= self._MIN_MAIN_CHARS
            else "".join(self._parts)
        )
        lines = [
            re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in source.splitlines()
        ]
        output: list[str] = []
        blank = False
        for line in lines:
            if line:
                output.append(line)
                blank = False
            elif not blank:
                output.append("")
                blank = True
        return "\n".join(output).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """(texto legível, título) de um documento HTML, sem script/style."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text(), " ".join(parser.title.split())


def is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global and not address.is_multicast


async def resolve_host(host: str) -> list[str]:
    infos = await asyncio.to_thread(
        socket.getaddrinfo, host, None, type=socket.SOCK_STREAM
    )
    return sorted({str(info[4][0]) for info in infos})


def _charset(content_type_header: str) -> str:
    for part in content_type_header.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip().strip('"').lower()
    return "utf-8"


@dataclass
class WebReferenceCollector:
    allowed_hosts: list[str] = field(default_factory=list)
    max_urls: int = 5
    max_bytes: int = 512_000
    max_chars: int = 12_000
    timeout_seconds: float = 10.0
    # PEM extra (CA corporativo de interceptação, por exemplo) somado ao
    # bundle padrão do certifi. Vazio = só certifi.
    ca_bundle: str | None = None
    client: httpx.AsyncClient | None = None
    resolver: Resolver = resolve_host

    def build_verify(self) -> ssl.SSLContext | bool:
        """Contexto TLS: certifi mais o bundle extra do operador, se houver."""
        if not self.ca_bundle:
            return True
        context = ssl.create_default_context(cafile=certifi.where())
        context.load_verify_locations(cafile=self.ca_bundle)
        return context

    @classmethod
    def from_settings(cls, settings: Settings) -> WebReferenceCollector:
        """Um coletor com os limites e a allowlist WEB_REFERENCES_* do operador.
        Compartilhado pela peça 1 (URLs do pedido) e pela ferramenta fetch_url."""
        return cls(
            allowed_hosts=[
                host.strip()
                for host in settings.web_references_allowed_hosts.split(",")
                if host.strip()
            ],
            max_urls=settings.web_references_max_urls,
            max_bytes=settings.web_references_max_bytes,
            max_chars=settings.web_references_max_chars,
            timeout_seconds=settings.web_references_timeout_seconds,
            ca_bundle=settings.web_references_ca_bundle or None,
        )

    def host_allowed(self, host: str) -> bool:
        if not self.allowed_hosts:
            return True
        host = host.lower()
        for entry in self.allowed_hosts:
            suffix = entry.strip().lower().lstrip(".")
            if suffix and (host == suffix or host.endswith("." + suffix)):
                return True
        return False

    async def validate_url(self, url: str) -> str:
        """Levanta WebReferenceBlocked ou devolve o host validado."""
        parts = urlsplit(url)
        if parts.scheme not in _DEFAULT_PORTS:
            raise WebReferenceBlocked(f"esquema {parts.scheme!r} não permitido")
        host = (parts.hostname or "").lower()
        if not host:
            raise WebReferenceBlocked("URL sem host")
        if parts.username or parts.password:
            raise WebReferenceBlocked("credenciais embutidas na URL")
        port = parts.port or _DEFAULT_PORTS[parts.scheme]
        if port != _DEFAULT_PORTS[parts.scheme] and not self.allowed_hosts:
            raise WebReferenceBlocked(f"porta {port} fora do padrão sem allowlist")
        if not self.host_allowed(host):
            raise WebReferenceBlocked(f"host {host} fora da allowlist")
        addresses = await self.resolver(host)
        if not addresses:
            raise WebReferenceBlocked(f"host {host} não resolve")
        if any(not is_public_address(address) for address in addresses):
            raise WebReferenceBlocked(f"host {host} resolve para endereço não público")
        return host

    async def _download(self, url: str) -> tuple[str, httpx.Headers, bytes]:
        client = self.client or httpx.AsyncClient(
            timeout=self.timeout_seconds, verify=self.build_verify()
        )
        owns_client = self.client is None
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "text/html, text/plain;q=0.9, application/json;q=0.8, */*;q=0.1",
        }
        try:
            current = url
            for _ in range(_MAX_REDIRECTS + 1):
                await self.validate_url(current)
                async with client.stream(
                    "GET", current, headers=headers, follow_redirects=False
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise WebReferenceBlocked("redirecionamento sem Location")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_bytes:
                            break
                    return str(response.url), response.headers, bytes(body[: self.max_bytes])
            raise WebReferenceBlocked(f"mais de {_MAX_REDIRECTS} redirecionamentos")
        finally:
            if owns_client:
                await client.aclose()

    async def fetch(self, url: str) -> dict[str, Any]:
        """Um item de evidência. Erros viram status='error' e nunca levantam."""
        fetched_at = datetime.now(timezone.utc).isoformat()
        try:
            final_url, response_headers, body = await self._download(url)
            content_type_header = response_headers.get("content-type", "")
            content_type = content_type_header.split(";")[0].strip().lower()
            if not content_type.startswith(_TEXT_CONTENT_TYPES):
                raise WebReferenceBlocked(
                    f"content-type {content_type or 'desconhecido'} não é textual"
                )
            try:
                raw_text = body.decode(_charset(content_type_header), errors="replace")
            except LookupError:
                raw_text = body.decode("utf-8", errors="replace")
            title = ""
            if content_type in _HTML_CONTENT_TYPES:
                text, title = html_to_text(raw_text)
            else:
                text = raw_text.strip()
            truncated = len(text) > self.max_chars
            if truncated:
                text = text[: self.max_chars].rstrip() + _TRUNCATION_MARKER
            return {
                "url": url,
                "final_url": final_url,
                "title": title,
                "content_type": content_type,
                "status": "ok",
                "chars": len(text),
                "truncated": truncated,
                "excerpt": text,
                "fetched_at": fetched_at,
            }
        except Exception as exc:  # noqa: BLE001 — falha de busca é evidência, não crash
            error = f"{type(exc).__name__}: {exc}"[:240]
            if "CERTIFICATE_VERIFY_FAILED" in error:
                error += " (CA não confiado pelo certifi; veja WEB_REFERENCES_CA_BUNDLE)"
            return {
                "url": url,
                "status": "error",
                "error": error,
                "excerpt": "",
                "fetched_at": fetched_at,
            }

    async def collect(self, request: str) -> dict[str, Any] | None:
        """Bloco `web_references` do contexto, ou None se o pedido não tem URL."""
        urls = extract_urls(request)
        if not urls:
            return None
        selected = urls[: self.max_urls]
        items = await asyncio.gather(*(self.fetch(url) for url in selected))
        evidence = [
            {"id": f"W{index}", **item} for index, item in enumerate(items, start=1)
        ]
        return {
            "source": "web_references",
            "require_citations": True,
            "omitted_urls": urls[self.max_urls :],
            "evidence": evidence,
        }
