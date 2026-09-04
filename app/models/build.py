"""Perfis administrados de build: dados imutáveis, nunca shell do repositório."""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.architecture import ArchitecturePolicy


class BuildPhaseName(str, Enum):
    PREPARE = "prepare"
    BUILD = "build"
    TEST = "test"
    LINT = "lint"
    TYPES = "types"


# Caminhos são absolutos dentro da imagem; o PATH do checkout nunca resolve
# executáveis. A aprovação final compara TODO o argv com o perfil selecionado.
BUILD_EXECUTABLES = frozenset(
    {"python", "python3", "pytest", "ruff", "mypy", "uv", "node", "tsc", "eslint"}
)
BUILD_ENVIRONMENT_KEYS = frozenset(
    {
        "CI",
        "LANG",
        "LC_ALL",
        "TZ",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHASHSEED",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "NODE_ENV",
        "NO_COLOR",
        "FORCE_COLOR",
    }
)
_SHELL_SYNTAX = re.compile(r"[\x00-\x1f\x7f;&|<>`$\\]")


def validate_command_token(value: str) -> str:
    if not value or len(value) > 4096 or _SHELL_SYNTAX.search(value):
        raise ValueError("Token de comando vazio, excessivo ou com sintaxe shell.")
    return value


def validate_relative_directory(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or value != path.as_posix()
        or _SHELL_SYNTAX.search(value)
        or ":" in value
    ):
        raise ValueError("cwd deve ser relativo, normalizado e dentro da lease.")
    return value


def argument_path(token: str) -> str | None:
    """Extrai valores de flags também; não permite caminhos externos ao checkout."""
    if token.startswith("-") and "=" not in token:
        # Flags com valor de caminho colado (por exemplo -I/tmp) não podem
        # contornar a inspeção. O operador deve usar tokens separados ou =.
        if any(marker in token for marker in ("/", "~", ":", "..")):
            raise ValueError("Caminhos em flags devem usar um argumento separado ou =.")
        return None
    value = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
    if "=" in value:
        raise ValueError("Argumentos compostos com atribuições não são suportados.")
    if (
        value.startswith(("/", "~"))
        or ":" in value
        or ".." in PurePosixPath(value).parts
    ):
        raise ValueError("Argumento contém caminho externo ou traversal.")
    return value


class BuildPhase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    name: BuildPhaseName
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    cwd: str = "."
    environment: dict[str, str] = Field(default_factory=dict)
    network: Literal["none", "dependencies"] = "none"
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    output_limit: int = Field(default=12_000, ge=256, le=100_000)

    @field_validator("argv")
    @classmethod
    def _argv_is_explicit(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for token in value:
            validate_command_token(token)
        for token in value[1:]:
            argument_path(token)
        executable = PurePosixPath(value[0])
        if (
            not executable.is_absolute()
            or value[0].startswith("//")
            or executable.as_posix() != value[0]
            or ".." in executable.parts
            or executable.name not in BUILD_EXECUTABLES
            or value[0].startswith("/workspace/")
        ):
            raise ValueError(
                "Executável deve ser um caminho absoluto aprovado da imagem."
            )
        return value

    @field_validator("cwd")
    @classmethod
    def _cwd_is_relative(cls, value: str) -> str:
        return validate_relative_directory(value)

    @field_validator("environment")
    @classmethod
    def _environment_has_no_secrets(cls, value: dict[str, str]) -> dict[str, str]:
        if not value.keys() <= BUILD_ENVIRONMENT_KEYS:
            raise ValueError("Variável de ambiente não permitida em fases de build.")
        for item in value.values():
            validate_command_token(item)
        return value

    @model_validator(mode="after")
    def _network_only_for_preparation(self) -> "BuildPhase":
        if self.network != "none" and self.name != BuildPhaseName.PREPARE:
            raise ValueError("Somente prepare pode solicitar rede para dependências.")
        return self


class AcceptanceCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    criterion: str = Field(min_length=1, max_length=500)
    command: BuildPhase
    expected_stdout: str = Field(max_length=8192)

    @model_validator(mode="after")
    def _bounded_case(self) -> "AcceptanceCase":
        if self.command.name != BuildPhaseName.TEST or self.command.network != "none":
            raise ValueError("Aceitação exige comando test sem rede.")
        if self.command.timeout_seconds > 30 or self.command.output_limit > 16_384:
            raise ValueError("Aceitação limitada a 30 segundos e 16 KiB de captura por caso.")
        if len(self.expected_stdout.encode()) > min(8192, self.command.output_limit):
            raise ValueError("Saída esperada excede o limite de captura.")
        return self

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()


class AcceptanceSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    cases: tuple[AcceptanceCase, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _bounded_suite(self) -> "AcceptanceSuite":
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("IDs de casos de aceitação devem ser únicos.")
        if sum(case.command.timeout_seconds for case in self.cases) > 120:
            raise ValueError("Suite excede 120 segundos de execução configurada.")
        if len(self.model_dump_json().encode()) > 64_000:
            raise ValueError("Suite excede 64 KB.")
        return self

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()


class BuildProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    ecosystem: Literal["python", "node"]
    image: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/:+-]*@sha256:[0-9a-f]{64}$")
    phases: tuple[BuildPhase, ...] = Field(min_length=1, max_length=5)
    auto_detect: bool = False
    architecture: ArchitecturePolicy | None = None
    acceptance: AcceptanceSuite | None = None

    @model_validator(mode="after")
    def _unique_ordered_phases(self) -> "BuildProfile":
        if self.architecture is not None and self.ecosystem != "python":
            raise ValueError("Architecture policies currently support Python only.")
        names = [phase.name for phase in self.phases]
        if len(names) != len(set(names)):
            raise ValueError("Fases de um perfil não podem se repetir.")
        if BuildPhaseName.PREPARE in names and names[0] != BuildPhaseName.PREPARE:
            raise ValueError("prepare deve ser a primeira fase do perfil.")
        return self

    def fingerprint(self) -> str:
        data = self.model_dump(mode="json")
        if self.architecture is None:
            data.pop("architecture")  # Preserve fingerprints of pre-policy profiles.
        if self.acceptance is None:
            data.pop("acceptance")
        payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()
