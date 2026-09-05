"""Credenciais de checkout: somente memória e processo Git remoto."""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.infrastructure.scm import TokenProvider
from app.models.factory import RepositoryTarget


@dataclass(frozen=True)
class GitAuthentication:
    source: str
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.source)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in self.source)
            or not self.token
            or any(c.isspace() or ord(c) < 32 for c in self.token)
        ):
            raise ValueError("Credencial ou destino Git inválido.")

    def _encoded(self) -> str:
        return base64.b64encode(f"x-access-token:{self.token}".encode()).decode()

    def environment(self) -> dict[str, str]:
        """Configuração efêmera; não usar -c, helper ou URL com senha."""
        prefix = f"http.{self.source}."
        pairs = [
            ("credential.helper", ""),
            ("core.hooksPath", os.devnull),
            ("init.templateDir", ""),
            ("protocol.allow", "never"),
            ("protocol.https.allow", "always"),
            ("fetch.recurseSubmodules", "false"),
            ("gc.auto", "0"),
            ("maintenance.auto", "false"),
            (prefix + "proxy", ""),
            (prefix + "sslVerify", "true"),
            (prefix + "followRedirects", "false"),
            (prefix + "extraHeader", ""),
            (prefix + "extraHeader", f"Authorization: Basic {self._encoded()}"),
        ]
        environment = {"GIT_CONFIG_COUNT": str(len(pairs))}
        for index, (key, value) in enumerate(pairs):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
        return environment

    def redact(self, value: str) -> str:
        # Redact complete output before truncation, including a Basic echo.
        return value.replace(self._encoded(), "***").replace(self.token, "***")


class GitHubRepositoryAccess:
    """Obtém credencial renovável apenas para o repositório GitHub solicitado."""

    def __init__(self, token_provider: TokenProvider) -> None:
        self._provider = token_provider

    async def for_repository(
        self, repository: RepositoryTarget, source: str
    ) -> GitAuthentication | None:
        if repository.scm_host != "github.com":
            return None
        expected = f"https://github.com/{repository.full_name}.git"
        if (
            source != expected
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository.full_name)
            is None
            or any(part in {".", ".."} for part in repository.full_name.split("/"))
        ):
            raise ValueError("Destino da credencial GitHub não corresponde à ordem.")
        try:
            token = await self._provider.token()
            return GitAuthentication(source, token)
        except Exception:
            # Providers may include response bodies in their exceptions.
            raise RuntimeError(
                "Não foi possível obter credencial GitHub para checkout."
            ) from None
