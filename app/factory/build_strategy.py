"""Seleção determinística de comandos aprovados sem executar o repositório."""

from __future__ import annotations

import json
import os
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.models.build import BuildProfile
from app.models.factory import BuildProfileSelection, WorkOrder, WorkspaceLease

MAX_MANIFEST_BYTES = 128 * 1024


@dataclass(frozen=True)
class _Manifest:
    present: bool
    error: str | None = None


class BuildProfileRegistry:
    """Guarda cópias dos perfis de operador e nunca importa comandos de manifests."""

    def __init__(
        self,
        profiles: Mapping[str, BuildProfile],
        repository_profiles: Mapping[str, str] | None = None,
    ) -> None:
        self._profiles: dict[str, BuildProfile] = {}
        for name, profile in profiles.items():
            if name != profile.name:
                raise ValueError("a chave do perfil deve corresponder ao seu nome.")
            # frozen não protege dictionaries internos nem model_copy(update=...).
            # Revalidar o dump evita promover modelos adulterados a política ativa.
            self._profiles[name] = BuildProfile.model_validate(profile.model_dump())

        self._repository_profiles: dict[str, str] = {}
        for repository, profile_name in (repository_profiles or {}).items():
            parts = repository.split("/")
            if len(parts) != 2 or any(
                not part or any(character.isspace() for character in part)
                for part in parts
            ):
                raise ValueError("o mapeamento deve usar repositórios owner/repo.")
            if profile_name not in self._profiles:
                raise ValueError(f"perfil mapeado desconhecido: {profile_name}.")
            key = repository.casefold()
            existing = self._repository_profiles.get(key)
            if existing is not None and existing != profile_name:
                raise ValueError("mapeamentos conflitantes para o mesmo repositório.")
            self._repository_profiles[key] = profile_name

    def get(self, name: str) -> BuildProfile:
        """Retorna uma cópia; alterações do chamador não mudam a política ativa."""
        try:
            return self._profiles[name].model_copy(deep=True)
        except KeyError as error:
            raise ValueError(f"perfil de build desconhecido: {name}.") from error

    def profile_for(self, selection: BuildProfileSelection) -> BuildProfile:
        """Reconstrói somente a configuração exata registrada na seleção."""
        if (
            selection.selection_reason
            not in {"explicit", "repository_mapping", "detected"}
            or selection.selected_profile is None
        ):
            raise ValueError("a seleção não contém um perfil de build suportado.")
        profile = self.get(selection.selected_profile)
        if selection.profile_digest != profile.fingerprint():
            raise ValueError(
                "o fingerprint do perfil mudou; uma nova seleção é necessária."
            )
        expected_architecture = (
            profile.architecture.fingerprint() if profile.architecture else None
        )
        if selection.architecture_digest != expected_architecture:
            raise ValueError(
                "O fingerprint da política de arquitetura diverge da seleção."
            )
        suite = profile.acceptance
        if (
            selection.acceptance_digest != (suite.fingerprint() if suite else None)
            or selection.acceptance_cases != (
                {case.id: case.fingerprint() for case in suite.cases} if suite else {}
            )
            or (suite is None and selection.acceptance_criteria)
            or (suite is not None and (
                not selection.acceptance_criteria
                or not set(selection.acceptance_criteria) <= {case.criterion for case in suite.cases}
            ))
        ):
            raise ValueError("Contrato de aceitação diverge da seleção ou não cobre os critérios.")
        if selection.phases != [phase.name.value for phase in profile.phases]:
            raise ValueError(
                "as fases registradas não correspondem ao perfil aprovado."
            )
        return profile

    def select(self, order: WorkOrder, lease: WorkspaceLease) -> BuildProfileSelection:
        selected = self._select(order, lease)
        if selected.selected_profile is not None:
            suite = self.get(selected.selected_profile).acceptance
            if suite is not None:
                if not set(order.acceptance_criteria) <= {case.criterion for case in suite.cases}:
                    return self._unsupported(
                        selected.requested_profile,
                        "A suite de aceitação não cobre todos os critérios aprovados da ordem.",
                    )
                selected = selected.model_copy(update={
                    "acceptance_digest": suite.fingerprint(),
                    "acceptance_cases": {case.id: case.fingerprint() for case in suite.cases},
                    "acceptance_criteria": list(order.acceptance_criteria),
                })
        return selected

    def _select(self, order: WorkOrder, lease: WorkspaceLease) -> BuildProfileSelection:
        """Aplica explícito → mapeamento → detecção segura, falhando sem fallback."""
        requested = order.build_profile.requested_profile
        target = order.repository
        provisioned = lease.repository
        if (
            target.scm_host.casefold() != provisioned.scm_host.casefold()
            or target.full_name.casefold() != provisioned.full_name.casefold()
            or target.base_ref != provisioned.base_ref
        ):
            return self._unsupported(
                requested, "o repositório da workspace lease não corresponde à ordem."
            )
        root = Path(lease.local_path)
        try:
            root_mode = root.lstat().st_mode
        except OSError:
            return self._unsupported(
                requested, "o diretório do workspace não está disponível."
            )
        if not stat.S_ISDIR(root_mode):
            return self._unsupported(
                requested,
                "o workspace deve ser um diretório real, não um link simbólico.",
            )

        if requested is not None:
            return self._named_selection(requested, requested, "explicit")
        mapped = self._repository_profiles.get(target.full_name.casefold())
        if mapped is not None:
            return self._named_selection(mapped, requested, "repository_mapping")

        python_manifest = self._inspect_manifest(root, "pyproject.toml")
        node_manifest = self._inspect_manifest(root, "package.json")
        if python_manifest.present and node_manifest.present:
            return self._unsupported(
                requested,
                "detecção ambígua: pyproject.toml e package.json estão presentes; "
                "configure um perfil explícito ou um mapeamento de operador.",
            )
        for manifest in (python_manifest, node_manifest):
            if manifest.error is not None:
                return self._unsupported(requested, manifest.error)
        if not python_manifest.present and not node_manifest.present:
            return self._unsupported(
                requested,
                "nenhum manifesto pyproject.toml ou package.json foi encontrado.",
            )
        ecosystem = "python" if python_manifest.present else "node"
        candidates = [
            profile
            for profile in self._profiles.values()
            if profile.auto_detect and profile.ecosystem == ecosystem
        ]
        if not candidates:
            return self._unsupported(
                requested, f"nenhum perfil auto_detect aprovado para {ecosystem}."
            )
        if len(candidates) != 1:
            return self._unsupported(
                requested,
                f"mais de um perfil auto_detect para {ecosystem}; "
                "configure um perfil explícito ou um mapeamento de operador.",
            )
        return self._named_selection(candidates[0].name, requested, "detected")

    def _named_selection(
        self,
        name: str,
        requested: str | None,
        reason: Literal["explicit", "repository_mapping", "detected"],
    ) -> BuildProfileSelection:
        profile = self._profiles.get(name)
        if profile is None:
            return self._unsupported(
                requested, f"perfil de build desconhecido: {name}."
            )
        return BuildProfileSelection(
            requested_profile=requested,
            selected_profile=profile.name,
            selection_reason=reason,
            profile_digest=profile.fingerprint(),
            architecture_digest=profile.architecture.fingerprint()
            if profile.architecture
            else None,
            phases=[phase.name.value for phase in profile.phases],
        )

    @staticmethod
    def _unsupported(requested: str | None, reason: str) -> BuildProfileSelection:
        return BuildProfileSelection(
            requested_profile=requested,
            selection_reason="unsupported",
            unsupported_reason=reason,
        )

    @staticmethod
    def _inspect_manifest(root: Path, name: str) -> _Manifest:
        manifest = root / name
        try:
            metadata = manifest.lstat()
        except FileNotFoundError:
            return _Manifest(present=False)
        except OSError:
            return _Manifest(
                True, f"não foi possível inspecionar {name} com segurança."
            )
        if not stat.S_ISREG(metadata.st_mode):
            return _Manifest(
                True, f"{name} deve ser um arquivo regular, sem link simbólico."
            )
        if metadata.st_size > MAX_MANIFEST_BYTES:
            return _Manifest(True, f"{name} excede o limite de 128 KiB.")
        try:
            # NOFOLLOW evita seguir links trocados após lstat; NONBLOCK impede
            # bloqueio caso um arquivo regular seja substituído por FIFO.
            descriptor = os.open(manifest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return _Manifest(True, f"{name} não é um arquivo regular.")
                content = stream.read(MAX_MANIFEST_BYTES + 1)
            if len(content) > MAX_MANIFEST_BYTES:
                return _Manifest(True, f"{name} excede o limite de 128 KiB.")
            decoded = content.decode("utf-8")
            if name == "pyproject.toml":
                tomllib.loads(decoded)
            elif not isinstance(json.loads(decoded), dict):
                return _Manifest(True, f"{name} deve conter um objeto JSON.")
        except OSError:
            return _Manifest(True, f"não foi possível ler {name} com segurança.")
        except (ValueError, UnicodeError, RecursionError):
            return _Manifest(True, f"{name} contém TOML/JSON inválido.")
        return _Manifest(present=True)
