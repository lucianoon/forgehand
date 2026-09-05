"""Non-secret installation identity and local operational diagnostics."""

from __future__ import annotations

import asyncio
import hashlib
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import socket
from tempfile import NamedTemporaryFile
from typing import Any
from uuid import UUID, uuid4

from app.infrastructure.settings import Settings


@lru_cache(maxsize=1)
def application_source_digest() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for source in sorted(root.rglob("*.py")):
        digest.update(source.relative_to(root).as_posix().encode() + b"\0")
        digest.update(source.read_bytes() + b"\0")
    return digest.hexdigest()


def _workspace_identity(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    target = root / ".forgehand-installation-id"
    if target.is_symlink():
        raise ValueError("workspace_identity_invalid")
    if not target.exists():
        # Publish a complete value atomically, including when API and worker start
        # simultaneously against the same volume. Never replace another identity.
        with NamedTemporaryFile(mode="w", dir=root, prefix=".installation-", delete=False) as temporary:
            temporary.write(str(uuid4()))
            temporary.flush()
            os.fsync(temporary.fileno())
            candidate = Path(temporary.name)
        try:
            try:
                os.link(candidate, target)
            except FileExistsError:
                pass
        finally:
            candidate.unlink(missing_ok=True)
    if target.is_symlink() or target.stat().st_size > 64:
        raise ValueError("workspace_identity_invalid")
    return str(UUID(target.read_text().strip()))


def installation_descriptor(settings: Settings) -> dict[str, Any]:
    required = settings.factory_mode_enabled and settings.workflow_queue_backend == "postgres"
    revision = settings.forgehand_revision or None
    configuration: dict[str, Any] = {
        "workspace_root": str(Path(settings.factory_workspace_root).expanduser().resolve()),
        "workspace_identity": None,
        "source_digest": application_source_digest(),
        "build_profiles_digest": None,
        "queue_backend": settings.workflow_queue_backend,
        "checkpointer_backend": settings.checkpointer_backend,
        "command_backend": settings.factory_command_backend,
        "dependency_network_enabled": settings.factory_sandbox_network_enabled,
        "approved_scm_hosts": sorted(settings.factory_approved_scm_hosts),
        "docker_socket": settings.factory_docker_socket,
    }
    errors: list[str] = []
    if settings.factory_mode_enabled:
        try:
            configuration["workspace_identity"] = _workspace_identity(Path(configuration["workspace_root"]))
        except (OSError, ValueError):
            errors.append("workspace_identity_unavailable")
        profiles = {name: profile.fingerprint() for name, profile in settings.factory_build_profiles.items()}
        configuration["build_profiles_digest"] = hashlib.sha256(json.dumps(
            {"profiles": profiles, "repositories": settings.factory_repository_profiles},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        if required and not revision:
            errors.append("revision_missing")
    fingerprint = None
    if required and not errors:
        fingerprint = hashlib.sha256(json.dumps(
            {"version": 1, "revision": revision, **configuration},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
    return {"required": required, "revision": revision, "fingerprint": fingerprint,
            "configuration": configuration, "errors": errors}


def _docker_socket_accessible(path: str) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(path)
        return True
    except (OSError, AttributeError):
        return False


async def local_installation_checks(settings: Settings, descriptor: dict[str, Any]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    def add(name: str, passed: bool, code: str, *, warning: bool = False) -> None:
        checks.append({"name": name, "status": "pass" if passed else "warning" if warning else "fail", "code": "ok" if passed else code})

    add("revision", bool(descriptor["revision"]), "revision_missing", warning=not descriptor["required"])
    if not settings.factory_mode_enabled:
        return checks
    add("workspace", bool(descriptor["configuration"]["workspace_identity"]), "workspace_identity_unavailable")
    add("git", shutil.which("git") is not None, "git_unavailable")
    add("docker_cli", shutil.which("docker") is not None, "docker_cli_unavailable")
    add("docker_socket", await asyncio.to_thread(_docker_socket_accessible, settings.factory_docker_socket), "docker_socket_unavailable")
    checks[-1]["detail"] = "Verifica conexão ao socket; o mount do daemon exige um ensaio de build."
    add("build_profiles", bool(settings.factory_build_profiles), "build_profiles_missing")
    key_path = os.getenv("GITHUB_APP_PRIVATE_KEY_PATH")
    key_file_present = bool(key_path and Path(key_path).is_file() and os.access(key_path, os.R_OK))
    app_credentials = bool(os.getenv("GITHUB_APP_ID") and os.getenv("GITHUB_APP_INSTALLATION_ID") and (
        os.getenv("GITHUB_APP_PRIVATE_KEY") or key_file_present
    ))
    add("github_credentials", bool(os.getenv("GITHUB_TOKEN")) or app_credentials, "github_credentials_missing")
    llm_key = {"openai": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[settings.llm_provider_backend]
    add("llm_credentials", bool(os.getenv(llm_key)), "llm_credentials_missing")
    return checks
