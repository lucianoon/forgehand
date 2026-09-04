"""Settings — toda configuração vem do ambiente (12-factor).

Aqui mora o que NUNCA deve estar em código: preços, bindings tier→modelo,
budgets default e backend de checkpoint. Trocar de modelo ou atualizar preço
é redeploy de config, não de código.
"""

from __future__ import annotations

import json
import ipaddress
import os
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agents.executor import ExecutionStrategy
from app.agents.hooks import ToolHookRule, parse_tool_hooks
from app.factory.build_strategy import BuildProfileRegistry
from app.models.build import BuildProfile
from app.providers.base import ModelPricing
from app.providers.registry import ModelTier, TierBinding

# Defaults de referência (jul/2026). VERIFICAR contra a página de preços
# oficial antes de produção — valores desatualizados corrompem o budget.
# Cache (Anthropic): escrita = 1.25x entrada, leitura = 0.10x entrada.
# OpenAI: a tarifa de leitura cacheada depende do modelo.
_DEFAULT_PRICING = {
    "claude-haiku-4-5": {
        "input_per_mtok": 1.0,
        "output_per_mtok": 5.0,
        "cache_write_per_mtok": 1.25,
        "cache_read_per_mtok": 0.10,
    },
    "claude-sonnet-5": {
        "input_per_mtok": 3.0,
        "output_per_mtok": 15.0,
        "cache_write_per_mtok": 3.75,
        "cache_read_per_mtok": 0.30,
    },
    "claude-opus-5": {
        "input_per_mtok": 5.0,
        "output_per_mtok": 25.0,
        "cache_write_per_mtok": 6.25,
        "cache_read_per_mtok": 0.50,
    },
    "openai/gpt-4o-mini": {
        "input_per_mtok": 0.15,
        "output_per_mtok": 0.60,
        "cache_read_per_mtok": 0.075,
    },
    # Official GPT-4.1 mini pricing checked on 2026-09-03 (standard text).
    "gpt-4.1-mini-2025-04-14": {
        "input_per_mtok": 0.40,
        "output_per_mtok": 1.60,
        "cache_read_per_mtok": 0.10,
    },
}

_DEFAULT_BINDINGS = {
    "1": {"provider_name": "anthropic", "model": "claude-haiku-4-5"},
    "2": {"provider_name": "anthropic", "model": "claude-sonnet-5"},
    "3": {"provider_name": "anthropic", "model": "claude-opus-5"},
}

_DEFAULT_OPENROUTER_BINDINGS = {
    "1": {"provider_name": "openrouter", "model": "openai/gpt-4o-mini"},
    "2": {"provider_name": "openrouter", "model": "openai/gpt-4o-mini"},
    "3": {"provider_name": "openrouter", "model": "openai/gpt-4o-mini"},
}

# Bounded pilot: no implicit upgrade to a more expensive model.
_DEFAULT_OPENAI_BINDINGS = {
    str(tier): {"provider_name": "openai", "model": "gpt-4.1-mini-2025-04-14"}
    for tier in range(1, 4)
}

_DEFAULT_API_KEYS = {
    "dev-key": {"client_id": "dev-client", "projects": ["*"]},
}

_DEFAULT_OBJECTIVE_VALIDATION_PIPELINES = {
    "backend": ["ruff", "mypy", "pytest"],
    "frontend": ["pytest"],
    "testing": ["ruff", "mypy", "pytest"],
    "review": ["ruff", "mypy", "pytest"],
    "research": [],
    "documentation": [],
    "devops": ["ruff", "mypy", "pytest"],
    "architecture": ["ruff", "mypy", "pytest"],
    "security": ["ruff", "mypy", "pytest"],
}

_DEFAULT_EXECUTION_STRATEGIES = {
    "backend": {
        "apply_files": True,
        "run_objective_validation": True,
        "allow_autocorrect": True,
    },
    "frontend": {
        "apply_files": True,
        "run_objective_validation": True,
        "allow_autocorrect": True,
    },
    "testing": {
        "apply_files": True,
        "run_objective_validation": True,
        "allow_autocorrect": True,
    },
    "review": {
        "apply_files": False,
        "run_objective_validation": False,
        "allow_autocorrect": False,
    },
    "research": {
        "apply_files": False,
        "run_objective_validation": False,
        "allow_autocorrect": False,
    },
    "documentation": {
        "apply_files": True,
        "run_objective_validation": False,
        "allow_autocorrect": False,
    },
    "devops": {
        "apply_files": True,
        "run_objective_validation": True,
        "allow_autocorrect": True,
    },
    "architecture": {
        "apply_files": True,
        "run_objective_validation": True,
        "allow_autocorrect": False,
    },
    "security": {
        "apply_files": True,
        "run_objective_validation": True,
        "allow_autocorrect": True,
    },
}

_DEFAULT_FACTORY_APPROVED_SCM_HOSTS = ["github.com"]


class ApiKeySettings(BaseModel):
    client_id: str = Field(min_length=1)
    projects: list[str] = Field(min_length=1)
    role: Literal["viewer", "operator", "approver", "admin"] = "admin"


def resolve_env_file() -> str | None:
    """Arquivo .env lido pelas Settings.

    Padrão: `.env` do diretório atual. FORGEHAND_ENV_FILE aponta para outro
    arquivo; vazio desliga a leitura — a suíte de testes usa isso para não
    herdar o .env do operador (backends, caminhos e chaves reais).
    """
    value = os.environ.get("FORGEHAND_ENV_FILE")
    if value is None:
        return ".env"
    return value or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=resolve_env_file(), env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "forgehand"
    environment: Literal["dev", "staging", "prod"] = "dev"
    llm_provider_backend: Literal["anthropic", "openrouter", "openai"] = "anthropic"
    api_keys_json: str = json.dumps(_DEFAULT_API_KEYS)
    workflow_queue_backend: Literal["memory", "postgres"] = "memory"
    run_embedded_workflow_workers: bool = True
    openrouter_base_url: str = "https://openrouter.ai/api"
    openrouter_supports_json_schema: bool = True
    openrouter_require_parameters: bool = True
    openrouter_response_healing: bool = True
    openrouter_prompt_caching: bool = True
    openrouter_http_referer: str | None = None
    openrouter_app_name: str = "forgehand"

    # Checkpointer: memory para dev/testes, postgres para qualquer coisa séria
    checkpointer_backend: Literal["memory", "postgres"] = "memory"
    database_url: str = "postgresql://forge:forge@localhost:5432/forgehand"

    # Memória de projeto (Fase 4): memory para dev/testes, neo4j para
    # histórico que sobrevive a restart. NEO4J_PASSWORD é lido direto do
    # ambiente pelo project_memory_context — segredo não passa por Settings.
    memory_backend: Literal["memory", "neo4j"] = "memory"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_database: str = "neo4j"
    memory_recent_workflows_limit: int = Field(default=5, ge=1, le=20)

    # Tracing (Fase 7): otlp cobre qualquer backend OTel, incluindo Langfuse.
    # Endpoint e credenciais via variáveis padrão do OTel
    # (OTEL_EXPORTER_OTLP_ENDPOINT/HEADERS) — lidas pelo exporter, não por
    # Settings: settings são logáveis, segredos não.
    tracing_backend: Literal["none", "otlp"] = "none"
    tracing_service_name: str = "forgehand"

    # ANTHROPIC_API_KEY é lida pelo SDK direto do ambiente — não passa por aqui
    # de propósito: settings são logáveis, segredos não.

    pricing_json: str = json.dumps(_DEFAULT_PRICING)
    tier_bindings_json: str = json.dumps(_DEFAULT_BINDINGS)
    # Judge independente do executor. JUDGE_TIER_BINDINGS_JSON sobrepõe o
    # binding do tier só para o papel "judge" (ex.: outro fornecedor/modelo).
    # judge_independence: "bindings" usa esses bindings e apenas REGISTRA se o
    # modelo coincidiu com o do executor; "escalate" troca de tier para
    # garantir modelo diferente (custa mais); "off" desliga a checagem.
    # Tarefas críticas recebem `judge_critical_quorum` julgamentos e todos
    # precisam aprovar (1 desliga).
    judge_tier_bindings_json: str = "{}"
    judge_independence: Literal["off", "bindings", "escalate"] = "bindings"
    judge_critical_quorum: int = Field(default=2, ge=1, le=3)

    # Budgets default do workflow (sobrescrevíveis por request)
    default_max_tokens: int = Field(default=500_000, gt=0)
    default_max_cost_usd: float = Field(default=5.0, gt=0)
    default_max_iterations: int = Field(default=3, ge=1)
    default_max_wall_clock_seconds: int = Field(default=1800, gt=0)
    default_task_max_tokens: int = Field(default=100_000, gt=0)
    default_task_max_cost_usd: float = Field(default=3.0, gt=0)
    workflow_worker_concurrency: int = Field(default=2, ge=1)
    workflow_start_timeout_seconds: float = Field(default=5.0, gt=0)
    workflow_queue_poll_interval_seconds: float = Field(default=0.25, gt=0)
    workflow_queue_lease_seconds: float = Field(default=30.0, gt=0)
    workflow_queue_max_delivery_attempts: int = Field(default=3, ge=1)
    audit_log_max_events: int = Field(default=500, ge=50)
    audit_log_backend: Literal["memory", "jsonl"] = "memory"
    audit_log_path: str = "./data/audit.jsonl"
    webhook_urls_json: str = "[]"
    repository_root: str = "."
    repository_grounding_enabled: bool = True
    repository_grounding_max_files: int = Field(default=16, ge=4, le=64)
    repository_grounding_max_lines_per_file: int = Field(default=60, ge=10, le=200)
    repository_grounding_max_file_bytes: int = Field(default=64_000, ge=4_096)
    # Arquivos até este tamanho entram INTEIROS na evidência (em vez do recorte
    # de max_lines_per_file). Necessário para op=replace em arquivos pequenos:
    # o executor só pode substituir trechos que viu. 0 desliga.
    repository_grounding_full_file_max_bytes: int = Field(default=0, ge=0)
    # Tool-use dos agentes: exploração limitada do workspace (read_file,
    # list_directory, search_repository e, para o executor, run_check).
    # max_calls=0 desliga para aquele papel; enabled=False desliga tudo.
    agent_tools_enabled: bool = True
    agent_tools_max_calls_executor: int = Field(default=8, ge=0, le=32)
    agent_tools_max_calls_planner: int = Field(default=4, ge=0, le=32)
    agent_tools_max_calls_judge: int = Field(default=4, ge=0, le=32)
    agent_tools_max_output_chars: int = Field(default=12_000, ge=1_000, le=100_000)
    agent_tools_allow_checks: bool = True
    tool_hooks_json: str = "[]"
    tool_hooks_timeout_seconds: float = Field(default=2.0, gt=0, le=10)

    @field_validator("tool_hooks_json")
    @classmethod
    def validate_tool_hooks(cls, value: str) -> str:
        parse_tool_hooks(value)
        return value

    @property
    def tool_hooks(self) -> tuple[ToolHookRule, ...]:
        return parse_tool_hooks(self.tool_hooks_json)

    # Entrega (PR + CI): cadência de polling dos checks e quanto esperar por
    # um primeiro check antes de concluir que o repositório não tem CI.
    # Credenciais (GITHUB_TOKEN ou GITHUB_APP_*) vêm do ambiente, não daqui.
    delivery_checks_poll_interval_seconds: float = Field(default=15.0, gt=0)
    delivery_checks_grace_seconds: float = Field(default=90.0, ge=0)
    # Diretório dedicado, criado sob demanda. "." exporia o cwd do servidor
    # (o próprio Forgehand) à exploração e escrita dos agentes.
    executor_workspace_root: str = "./data/executor-workspace"
    executor_apply_files_enabled: bool = False
    executor_command_backend: Literal["local", "docker"] = "local"
    executor_sandbox_image: str = "python:3.12-slim"
    executor_sandbox_memory: str = "512m"
    executor_sandbox_cpus: float = Field(default=1.0, gt=0, le=8)
    executor_sandbox_network_enabled: bool = False
    executor_max_autocorrect_rounds: int = Field(default=0, ge=0, le=5)
    pytest_validation_command: str | None = None
    ruff_validation_command: str | None = None
    mypy_validation_command: str | None = None
    objective_validation_pipelines_json: str = json.dumps(
        _DEFAULT_OBJECTIVE_VALIDATION_PIPELINES
    )
    executor_strategies_json: str = json.dumps(_DEFAULT_EXECUTION_STRATEGIES)

    # Software factory: opt-in e isolada do caminho legado. Perfis e
    # associações são administrados; conteúdo do repositório não injeta shell.
    factory_mode_enabled: bool = False
    product_studio_enabled: bool = False
    product_studio_database: str = "data/product-studio.sqlite3"
    factory_approved_scm_hosts_json: str = json.dumps(
        _DEFAULT_FACTORY_APPROVED_SCM_HOSTS
    )
    factory_workspace_root: str = "./data/factory-workspaces"
    factory_docker_socket: str = "/var/run/docker.sock"

    @field_validator("factory_docker_socket")
    @classmethod
    def validate_factory_docker_socket(cls, value: str) -> str:
        # Socket Unix: caminho POSIX absoluto. PurePosixPath mantém o default
        # válido também no Windows, onde Path("/var/run/...") não é absoluto e
        # a factory (que nem roda lá) derrubaria Settings() inteiro.
        if not (Path(value).is_absolute() or PurePosixPath(value).is_absolute()):
            raise ValueError("FACTORY_DOCKER_SOCKET must be an absolute local path")
        return value

    factory_success_retention_seconds: int = Field(default=0, ge=0)
    factory_failure_retention_seconds: int = Field(default=86_400, ge=0)
    factory_command_backend: Literal["local", "docker"] = "docker"
    factory_sandbox_image: str = "python:3.12-slim"
    factory_sandbox_network_enabled: bool = False
    factory_build_profiles_json: str = "{}"
    factory_repository_profiles_json: str = "{}"

    @field_validator(
        "pricing_json",
        "tier_bindings_json",
        "judge_tier_bindings_json",
        "api_keys_json",
        "objective_validation_pipelines_json",
        "executor_strategies_json",
        "webhook_urls_json",
        "factory_approved_scm_hosts_json",
        "factory_build_profiles_json",
        "factory_repository_profiles_json",
    )
    @classmethod
    def _must_be_json(cls, v: str) -> str:
        json.loads(v)
        return v

    @model_validator(mode="after")
    def _validate_security(self) -> "Settings":
        if self.environment == "prod" and self.api_keys_json == json.dumps(
            _DEFAULT_API_KEYS
        ):
            raise ValueError("Defina API_KEYS_JSON diferente do default em produção.")
        if (
            self.workflow_queue_backend == "postgres"
            and self.checkpointer_backend != "postgres"
        ):
            raise ValueError(
                "WORKFLOW_QUEUE_BACKEND=postgres requer CHECKPOINTER_BACKEND=postgres."
            )
        if (
            not self.run_embedded_workflow_workers
            and self.workflow_queue_backend != "postgres"
        ):
            raise ValueError("Workers externos exigem WORKFLOW_QUEUE_BACKEND=postgres.")
        if self.factory_mode_enabled:
            BuildProfileRegistry(
                self.factory_build_profiles, self.factory_repository_profiles
            )
            if not self.factory_approved_scm_hosts:
                raise ValueError("Factory mode exige ao menos um host SCM aprovado.")
            root = Path(self.factory_workspace_root).expanduser()
            if str(root) in {".", "/"}:
                raise ValueError(
                    "FACTORY_WORKSPACE_ROOT deve ser um diretório dedicado."
                )
            if self.factory_command_backend != "docker":
                raise ValueError(
                    "Factory mode exige backend docker; execução local é apenas legada."
                )
        return self

    @property
    def pricing(self) -> dict[str, ModelPricing]:
        raw = json.loads(self.pricing_json)
        return {model: ModelPricing(**p) for model, p in raw.items()}

    @property
    def tier_bindings(self) -> dict[ModelTier, TierBinding]:
        raw = json.loads(self.tier_bindings_json)
        if self.llm_provider_backend == "openrouter" and raw == _DEFAULT_BINDINGS:
            raw = _DEFAULT_OPENROUTER_BINDINGS
        elif self.llm_provider_backend == "openai" and raw == _DEFAULT_BINDINGS:
            raw = _DEFAULT_OPENAI_BINDINGS
        return {ModelTier(int(k)): TierBinding(**v) for k, v in raw.items()}

    @property
    def judge_tier_bindings(self) -> dict[ModelTier, TierBinding]:
        raw = json.loads(self.judge_tier_bindings_json)
        return {ModelTier(int(k)): TierBinding(**v) for k, v in raw.items()}

    @property
    def api_keys(self) -> dict[str, ApiKeySettings]:
        raw = json.loads(self.api_keys_json)
        return {key: ApiKeySettings(**value) for key, value in raw.items()}

    @property
    def objective_validation_pipelines(self) -> dict[str, list[str]]:
        raw = json.loads(self.objective_validation_pipelines_json)
        return {
            str(capability): [str(name) for name in names]
            for capability, names in raw.items()
        }

    @property
    def executor_strategies(self) -> dict[str, ExecutionStrategy]:
        raw = json.loads(self.executor_strategies_json)
        return {
            str(capability): ExecutionStrategy.model_validate(config)
            for capability, config in raw.items()
        }

    @property
    def webhook_urls(self) -> list[str]:
        return [str(url) for url in json.loads(self.webhook_urls_json)]

    @property
    def factory_approved_scm_hosts(self) -> list[str]:
        raw = json.loads(self.factory_approved_scm_hosts_json)
        if not isinstance(raw, list):
            raise ValueError("FACTORY_APPROVED_SCM_HOSTS_JSON deve ser uma lista.")
        hosts: list[str] = []
        for item in raw:
            host = str(item).strip().lower().rstrip(".")
            if not host or "*" in host or "://" in host or "/" in host:
                raise ValueError(f"Host SCM inválido: {item!r}.")
            if host == "localhost":
                raise ValueError("localhost não pode ser host SCM aprovado.")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError("Endereços IP não podem ser hosts SCM aprovados.")
            hosts.append(host)
        return hosts

    @property
    def factory_build_profiles(self) -> dict[str, BuildProfile]:
        raw = json.loads(self.factory_build_profiles_json)
        if not isinstance(raw, dict):
            raise ValueError("FACTORY_BUILD_PROFILES_JSON deve ser um objeto.")
        profiles: dict[str, BuildProfile] = {}
        for name, value in raw.items():
            if not isinstance(value, dict):
                raise ValueError("Cada perfil de build deve ser um objeto.")
            if "name" in value and value["name"] != name:
                raise ValueError("Nome do perfil diverge da chave administrada.")
            profiles[name] = BuildProfile.model_validate({"name": name, **value})
        return profiles

    @property
    def factory_repository_profiles(self) -> dict[str, str]:
        raw = json.loads(self.factory_repository_profiles_json)
        if not isinstance(raw, dict):
            raise ValueError("FACTORY_REPOSITORY_PROFILES_JSON deve ser um objeto.")
        return {str(repository): str(profile) for repository, profile in raw.items()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
