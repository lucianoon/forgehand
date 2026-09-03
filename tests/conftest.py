"""Configuração hermética da suíte.

Variáveis do shell e o .env de desenvolvimento não podem trocar providers,
habilitar escrita no workspace ou tornar testes dependentes de serviços reais.
"""

from __future__ import annotations

import json
import os


_TEST_ENV = {
    "LLM_PROVIDER_BACKEND": "anthropic",
    "CHECKPOINTER_BACKEND": "memory",
    "WORKFLOW_QUEUE_BACKEND": "memory",
    "RUN_EMBEDDED_WORKFLOW_WORKERS": "true",
    "REPOSITORY_GROUNDING_ENABLED": "false",
    "EXECUTOR_APPLY_FILES_ENABLED": "false",
    "EXECUTOR_MAX_AUTOCORRECT_ROUNDS": "0",
    "FACTORY_MODE_ENABLED": "false",
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
