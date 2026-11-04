"""Entrega local — o artefato que sobrevive ao processo.

Sem Postgres e sem GitHub, a análise morria no `final_output` da RAM.
Aqui o desfecho vira arquivos em disco: `ENTREGA.md` mais o que as tarefas
aprovaram (`published_files`). O PR no GitHub, quando existir, é o segundo
destino — este é o primeiro, sempre.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.graph.state import WorkflowState
from app.infrastructure.scm import collect_publishable_changes

logger = logging.getLogger("forgehand")

_ENTREGA = "ENTREGA.md"


def delivery_dir(root: str | Path, workflow_id: str) -> Path:
    return Path(root).expanduser().resolve() / workflow_id


def persist_delivery(state: WorkflowState, root: str | Path) -> Path | None:
    """Grava a entrega do workflow. Falha de I/O não propaga — o grafo
    já executou; perder o arquivo não pode marcar o job como failed."""
    text = (state.final_output or "").strip()
    files, _deletions = collect_publishable_changes(
        [{"result": t.result} for t in state.completed_tasks]
    )
    if not text and not files:
        return None
    target = delivery_dir(root, state.workflow_id)
    try:
        target.mkdir(parents=True, exist_ok=True)
        if text:
            (target / _ENTREGA).write_text(text + "\n", encoding="utf-8")
        for item in files:
            try:
                path = _safe_child(target, item["path"])
            except ValueError:
                logger.warning(
                    "Ignorando path fora da entrega de %s: %s",
                    state.workflow_id,
                    item["path"],
                )
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(item["content"], encoding="utf-8")
        return target
    except OSError:
        logger.exception("Falha ao gravar entrega de %s", state.workflow_id)
        return None


def _safe_child(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Path fora da entrega: {relative}")
    return candidate


class LocalArtifactStore:
    def __init__(self, root: str):
        self._root = root

    async def persist(self, state: WorkflowState) -> str | None:
        path = persist_delivery(state, self._root)
        return None if path is None else str(path)

    def path_for(self, workflow_id: str) -> str:
        return str(delivery_dir(self._root, workflow_id))
