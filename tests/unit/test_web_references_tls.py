"""Bundle de CA extra para buscas atrás de interceptação TLS corporativa."""

from __future__ import annotations

import ssl
from pathlib import Path

import certifi

from app.infrastructure.memory import InMemoryProjectMemory
from app.infrastructure.settings import Settings
from app.infrastructure.web_references import WebReferenceCollector


def test_default_verify_is_certifi_only() -> None:
    assert WebReferenceCollector().build_verify() is True


def test_ca_bundle_is_added_on_top_of_certifi(tmp_path: Path) -> None:
    # Um PEM válido qualquer serve para provar o carregamento: reusa o certifi.
    extra = tmp_path / "extra.pem"
    extra.write_bytes(Path(certifi.where()).read_bytes())
    collector = WebReferenceCollector(ca_bundle=str(extra))
    context = collector.build_verify()
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.get_ca_certs()


def test_settings_wire_ca_bundle_into_collector(tmp_path: Path) -> None:
    extra = tmp_path / "extra.pem"
    extra.write_bytes(Path(certifi.where()).read_bytes())
    memory = InMemoryProjectMemory(
        Settings(
            _env_file=None,
            repository_grounding_enabled=False,
            web_references_enabled=True,
            web_references_ca_bundle=str(extra),
        )
    )
    assert memory._web_collector is not None
    assert memory._web_collector.ca_bundle == str(extra)
