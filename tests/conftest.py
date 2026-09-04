"""Configuração hermética da suíte.

Variáveis do shell e o .env de desenvolvimento não podem trocar providers,
habilitar escrita no workspace ou tornar testes dependentes de serviços reais.
"""

from __future__ import annotations

import json
import os


_TEST_ENV = {
    # Sem isso, Settings() lê o .env do diretório atual e um AUDIT_LOG_PATH
    # ou backend real do operador derruba testes de API.
    "FORGEHAND_ENV_FILE": "",
    "LLM_PROVIDER_BACKEND": "anthropic",
    "AUDIT_LOG_BACKEND": "memory",
    "TRACING_BACKEND": "none",
    "CHECKPOINTER_BACKEND": "memory",
    "WORKFLOW_QUEUE_BACKEND": "memory",
    "RUN_EMBEDDED_WORKFLOW_WORKERS": "true",
    "REPOSITORY_GROUNDING_ENABLED": "false",
    "EXECUTOR_APPLY_FILES_ENABLED": "false",
    "EXECUTOR_MAX_AUTOCORRECT_ROUNDS": "0",
    "FACTORY_MODE_ENABLED": "false",
    "TOOL_HOOKS_JSON": "[]",
    "TOOL_HOOKS_TIMEOUT_SECONDS": "2",
    "PYTEST_VALIDATION_COMMAND": "",
    "RUFF_VALIDATION_COMMAND": "",
    "MYPY_VALIDATION_COMMAND": "",
    "TIER_BINDINGS_JSON": json.dumps(
        {
            "1": {"provider_name": "anthropic", "model": "claude-haiku-4-5"},
            "2": {"provider_name": "anthropic", "model": "claude-sonnet-5"},
            "3": {"provider_name": "anthropic", "model": "claude-opus-5"},
        }
    ),
}

os.environ.update(_TEST_ENV)
