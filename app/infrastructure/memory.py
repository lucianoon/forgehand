"""Memória de projeto — conhecimento que atravessa workflows (Fase 4).

O protocolo MemoryStore (app.graph.nodes) é a fronteira: load_context entrega
o histórico do projeto ao planner ("Contexto adicional do projeto" no prompt);
persist grava o desfecho de cada workflow ao final do grafo.

Dois backends, mesmo contrato:
- InMemoryProjectMemory — dev/testes, processo local;
- Neo4jProjectMemory — grafo persistente
  (Project)-[:HAS_WORKFLOW]->(Workflow)-[:EXECUTED]->(Task),
  sobrevive a restart e alimenta workflows futuros.

Decisões:
- o grounding do repositório é ortogonal ao backend — a coleta vive na base;
- memória é CONSELHEIRA: falha de leitura degrada para contexto vazio e falha
  de escrita é logada e engolida — nunca derruba um workflow que já executou;
- NEO4J_PASSWORD é lido direto do ambiente no context manager, nunca passa
  pelas Settings (settings são logáveis, segredos não).
"""

from __future__ import annotations

import logging
import os
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from app.graph.state import WorkflowState
from app.infrastructure.repository_grounding import RepositoryGroundingCollector
from app.infrastructure.settings import Settings

logger = logging.getLogger("forgehand")

_MAX_REQUEST_CHARS = 200
_MAX_OUTPUT_CHARS = 500


def workflow_summary(state: WorkflowState) -> dict[str, Any]:
    """O que fica registrado por workflow — o mesmo shape nos dois backends."""
    return {
        "workflow_id": state.workflow_id,
        "request": state.request[:_MAX_REQUEST_CHARS],
        "phase": state.phase.value,
        "iteration": state.iteration,
        "tokens": int(state.usage.get("tokens", 0)),
        "cost_usd": float(state.usage.get("cost_usd", 0.0)),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "final_output_excerpt": (state.final_output or "")[:_MAX_OUTPUT_CHARS],
        "tasks": [
            {
                "id": str(t.id),
                "title": t.title,
                "capability": t.capability.value,
                "status": t.status.value,
                "attempts": t.attempt_count,
            }
            for t in state.plan
        ],
    }


def _context_entry(summary: dict[str, Any]) -> dict[str, Any]:
    """Projeção enxuta para o prompt do planner — histórico informa, não
    inunda: sem excerpt de saída nem métricas de custo."""
    return {
        "request": summary.get("request"),
        "phase": summary.get("phase"),
        "finished_at": summary.get("finished_at"),
        "tasks": [
            {"title": t.get("title"), "status": t.get("status")}
            for t in summary.get("tasks", [])
        ],
    }


class BaseProjectMemory:
    """Grounding + template do load_context; subclasses só implementam
    o armazenamento (_recent_summaries / persist)."""

    def __init__(self, settings: Settings) -> None:
        self._collector = RepositoryGroundingCollector(
            settings.repository_root,
            max_files=settings.repository_grounding_max_files,
            max_excerpt_lines=settings.repository_grounding_max_lines_per_file,
            max_file_bytes=settings.repository_grounding_max_file_bytes,
            full_file_max_bytes=settings.repository_grounding_full_file_max_bytes,
        )
        self._grounding_enabled = settings.repository_grounding_enabled
        self._recent_limit = settings.memory_recent_workflows_limit

    async def load_context(self, project_id: str, request: str = "") -> dict[str, Any]:
        context = await self.load_project_context(project_id, request)
        if self._grounding_enabled:
            context["repository_grounding"] = self._collector.collect(request)
        return context

    async def load_project_context(
        self, project_id: str, request: str = ""
    ) -> dict[str, Any]:
        """Carrega memória histórica sem tocar em uma raiz de repositório.

        O modo fábrica usa esta variante e injeta grounding somente após a
        lease exclusiva ter sido provisionada.
        """
        context: dict[str, Any] = {}
        summaries = await self._recent_summaries(project_id)
        if summaries:
            context["last_workflow_id"] = summaries[0].get("workflow_id")
            context["project_memory"] = {
                "recent_workflows": [_context_entry(s) for s in summaries]
            }
        return context

    async def _recent_summaries(self, project_id: str) -> list[dict[str, Any]]:
        """Mais recente primeiro, no máximo _recent_limit itens."""
        raise NotImplementedError

    async def persist(self, state: WorkflowState) -> None:
        raise NotImplementedError


class InMemoryProjectMemory(BaseProjectMemory):
    """Dev/testes. Mesmo contrato do Neo4j, processo local."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._by_project: dict[str, deque[dict[str, Any]]] = {}

    async def _recent_summaries(self, project_id: str) -> list[dict[str, Any]]:
        return list(reversed(self._by_project.get(project_id, deque())))

    async def persist(self, state: WorkflowState) -> None:
        history = self._by_project.setdefault(
            state.project_id, deque(maxlen=self._recent_limit)
        )
        history.append(workflow_summary(state))


_PERSIST_QUERY = """
MERGE (p:Project {id: $project_id})
MERGE (w:Workflow {id: $workflow_id})
SET w.request = $request,
    w.phase = $phase,
    w.iteration = $iteration,
    w.tokens = $tokens,
    w.cost_usd = $cost_usd,
    w.finished_at = $finished_at,
    w.final_output_excerpt = $final_output_excerpt
MERGE (p)-[:HAS_WORKFLOW]->(w)
WITH w
UNWIND $tasks AS task
MERGE (t:Task {id: task.id})
SET t.title = task.title,
    t.capability = task.capability,
    t.status = task.status,
    t.attempts = task.attempts
MERGE (w)-[:EXECUTED]->(t)
"""

_LOAD_QUERY = """
MATCH (p:Project {id: $project_id})-[:HAS_WORKFLOW]->(w:Workflow)
OPTIONAL MATCH (w)-[:EXECUTED]->(t:Task)
WITH w, collect({title: t.title, status: t.status}) AS tasks
ORDER BY w.finished_at DESC
LIMIT $limit
RETURN w.id AS workflow_id,
       w.request AS request,
       w.phase AS phase,
       w.finished_at AS finished_at,
       tasks
"""


class Neo4jProjectMemory(BaseProjectMemory):
    """Memória persistente em grafo. O driver é INJETADO (como o client
    Anthropic no provider): testes passam um fake sem tocar em rede."""

    def __init__(self, driver: Any, settings: Settings) -> None:
        super().__init__(settings)
        self._driver = driver
        self._database = settings.neo4j_database

    async def _recent_summaries(self, project_id: str) -> list[dict[str, Any]]:
        try:
            result = await self._driver.execute_query(
                _LOAD_QUERY,
                parameters_={"project_id": project_id, "limit": self._recent_limit},
                database_=self._database,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Memória Neo4j indisponível na leitura do projeto %s; "
                "seguindo sem histórico.",
                project_id,
            )
            return []
        summaries = []
        for record in result.records:
            data = record.data()
            data["tasks"] = [
                t for t in data.get("tasks", []) if t.get("title") is not None
            ]
            summaries.append(data)
        return summaries

    async def persist(self, state: WorkflowState) -> None:
        summary = workflow_summary(state)
        try:
            await self._driver.execute_query(
                _PERSIST_QUERY,
                parameters_={"project_id": state.project_id, **summary},
                database_=self._database,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Memória Neo4j indisponível na escrita do workflow %s; "
                "o resultado do workflow NÃO foi afetado.",
                state.workflow_id,
            )


@asynccontextmanager
async def project_memory_context(
    settings: Settings,
) -> AsyncGenerator[BaseProjectMemory, None]:
    """O driver Neo4j é um recurso com lifecycle (pool de conexões) — mesmo
    padrão do checkpointer_context. Backend memory não tem recurso a fechar."""
    if settings.memory_backend == "neo4j":
        # import tardio: dependência opcional (extra [neo4j])
        from neo4j import AsyncGraphDatabase

        driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, os.getenv("NEO4J_PASSWORD", "")),
        )
        try:
            yield Neo4jProjectMemory(driver, settings)
        finally:
            await driver.close()
    else:
        yield InMemoryProjectMemory(settings)
