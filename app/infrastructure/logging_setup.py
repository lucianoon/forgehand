"""Logging do aplicativo sob uvicorn e no worker.

O uvicorn configura só os próprios loggers; o logger raiz fica sem handler e
tudo que o Forgehand emite abaixo de WARNING (custo e cache por chamada de
LLM, auditoria de hooks, referências web) desaparece. Configura o raiz uma
única vez, sem sobrescrever uma configuração que o operador já tenha feito.
"""

from __future__ import annotations

import logging
import os

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(level: str | None = None) -> None:
    root = logging.getLogger()
    if root.handlers:
        # Operador (ou testes) já configuraram: não duplicar handlers.
        return
    chosen = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    numeric = logging.getLevelName(chosen)
    logging.basicConfig(
        level=numeric if isinstance(numeric, int) else logging.INFO, format=_FORMAT
    )
    # Ruído de bibliotecas fica em WARNING; o app fala em INFO.
    for noisy in ("httpx", "httpcore", "anthropic", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
