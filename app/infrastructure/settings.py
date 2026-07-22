"""Settings — toda configuração vem do ambiente (12-factor).

Aqui mora o que NUNCA deve estar em código: preços, bindings tier→modelo,
budgets default e backend de checkpoint. Trocar de modelo ou atualizar preço
é redeploy de config, não de código.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agents.executor import ExecutionStrategy
from app.providers.base import ModelPricing
from app.providers.registry import ModelTier, TierBinding

# Defaults de referência (jul/2026). VERIFICAR contra a página de preços
# oficial antes de produção — valores desatualizados corrompem o budget.
_DEFAULT_PRICING = {
    "claude-haiku-4-5": {"input_per_mtok": 1.0, "output_per_mtok": 5.0},
    "claude-sonnet-4-6": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
    "claude-opus-4-8": {"input_per_mtok": 15.0, "output_per_mtok": 75.0},
    "openai/gpt-4o-mini": {"input_per_mtok": 0.15, "output_per_mtok": 0.60},
}

_DEFAULT_BINDINGS = {
    "1": {"provider_name": "anthropic", "model": "claude-haiku-4-5"},
    "2": {"provider_name": "anthropic", "model": "claude-sonnet-4-6"},
    "3": {"provider_name": "anthropic", "model": "claude-opus-4-8"},
}

_DEFAULT_OPENROUTER_BINDINGS = {
    "1": {"provider_name": "openrouter", "model": "openai/gpt-4o-mini"},
    "2": {"provider_name": "openrouter", "model": "openai/gpt-4o-mini"},
    "3": {"provider_name": "openrouter", "model": "openai/gpt-4o-mini"},
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


class ApiKeySettings(BaseModel):
    client_id: str = Field(min_length=1)
    projects: list[str] = Field(min_length=1)
    role: Literal["viewer", "operator", "approver", "admin"] = "admin"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "forgehand"
    environment: Literal["dev", "staging", "prod"] = "dev"
    llm_provider_backend: Literal["anthropic", "openrouter"] = "anthropic"
    api_keys_json: str = json.dumps(_DEFAULT_API_KEYS)
    workflow_queue_backend: Literal["memory", "postgres"] = "memory"
    run_embedded_workflow_workers: bool = True
    openrouter_base_url: str = "https://openrouter.ai/api"
    openrouter_supports_json_schema: bool = True
    openrouter_require_parameters: bool = True
    openrouter_response_healing: bool = True
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
    executor_workspace_root: str = "."
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

    @field_validator(
        "pricing_json",
        "tier_bindings_json",
        "api_keys_json",
        "objective_validation_pipelines_json",
        "executor_strategies_json",
        "webhook_urls_json",
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
