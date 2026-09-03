"""Autorização de comandos, independente de runners ou clientes LLM."""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.models.build import (
    BuildPhaseName,
    BuildProfile,
    argument_path,
    validate_relative_directory,
)


@dataclass(frozen=True)
class AuthorizedBuildCommand:
    profile_name: str
    profile_digest: str
    phase: BuildPhaseName
    image: str
    argv: tuple[str, ...]
    cwd: Path
    environment: tuple[tuple[str, str], ...]
    timeout_seconds: int
    output_limit: int
    network_enabled: bool


class CommandPolicy:
    def __init__(
        self,
        allowed_executables: set[str] | None = None,
        *,
        profile: BuildProfile | None = None,
    ) -> None:
        self._allowed = allowed_executables or {
            "git",
            "mypy",
            "pytest",
            "python",
            "python3",
            "ruff",
            "uv",
        }
        # Congela uma cópia validada do perfil do operador. Mudar o modelo
        # original ou seu dict environment não altera a política ativa.
        self._profile = (
            BuildProfile.model_validate(profile.model_dump()) if profile else None
        )

    @staticmethod
    def _normalize(executable: str) -> str:
        if os.name != "nt":
            return executable
        name = executable.lower()
        return name[:-4] if name.endswith(".exe") else name

    def parse(self, command: str) -> list[str]:
        """Compatibilidade legada. Factory usa validate_phase, não basename."""
        if self._profile is not None:
            raise ValueError("Perfil de fábrica exige autorização de fase completa.")
        argv = shlex.split(command)
        if not argv:
            raise ValueError("Comando vazio.")
        executable = Path(argv[0]).name
        if self._normalize(executable) not in self._allowed:
            raise ValueError(f"Executável não permitido: {executable}")
        return argv

    def validate_phase(
        self,
        phase_name: BuildPhaseName | str,
        workspace_root: Path,
        *,
        argv: Sequence[str] | None = None,
        environment: Mapping[str, str] | None = None,
        cwd: str | None = None,
        allow_dependency_network: bool = False,
    ) -> AuthorizedBuildCommand:
        """Autoriza somente o contrato exato do perfil, sem executar código.

        Overrides existem para verificar propostas vindas de ferramentas;
        nenhum deles pode ampliar os comandos/ambiente/cwd do operador.
        """
        profile = self._profile
        if profile is None:
            raise ValueError("Nenhum perfil administrado foi selecionado.")
        phase = next((item for item in profile.phases if item.name == phase_name), None)
        if phase is None:
            raise ValueError("Fase não existe no perfil selecionado.")
        actual_argv = phase.argv if argv is None else tuple(argv)
        actual_environment = (
            phase.environment if environment is None else dict(environment)
        )
        actual_cwd = phase.cwd if cwd is None else cwd
        if actual_argv != phase.argv:
            raise ValueError("Executável ou argumentos divergem do perfil aprovado.")
        if actual_environment != phase.environment:
            raise ValueError("Ambiente diverge do perfil aprovado.")
        if actual_cwd != phase.cwd:
            raise ValueError("cwd diverge do perfil aprovado.")
        validate_relative_directory(actual_cwd)
        root = workspace_root.expanduser()
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ValueError("Raiz da lease deve ser diretório absoluto, não symlink.")
        root = root.resolve()
        if root == Path(root.anchor):
            raise ValueError("Raiz global não pode ser usada como lease.")
        directory = (root / actual_cwd).resolve()
        if not directory.is_relative_to(root) or not directory.is_dir():
            raise ValueError("cwd não existe ou escapa da lease.")
        for token in actual_argv[1:]:
            value = argument_path(token)
            if value is not None and not (directory / value).resolve().is_relative_to(
                root
            ):
                raise ValueError("Argumento resolve para fora da lease.")
        network_enabled = phase.network == "dependencies"
        if network_enabled and not allow_dependency_network:
            raise ValueError(
                "dependency_preparation_not_authorized: rede não autorizada."
            )
        return AuthorizedBuildCommand(
            profile_name=profile.name,
            profile_digest=profile.fingerprint(),
            phase=phase.name,
            image=profile.image,
            argv=phase.argv,
            cwd=directory,
            environment=tuple(sorted(phase.environment.items())),
            timeout_seconds=phase.timeout_seconds,
            output_limit=phase.output_limit,
            network_enabled=network_enabled,
        )
