"""Serviço de workflows — a fronteira entre HTTP e o grafo.

WorkflowService:
- start(): enfileira o job; workers embutidos ou externos processam;
- get(): lê SEMPRE do checkpointer — a fonte de verdade é o checkpoint,
  não memória do processo. Sobrevive a restart quando o backend é Postgres;
- decide(): retoma um interrupt pendente com Command(resume=...) — a
  decisão humana entra pelo mesmo mecanismo de qualquer retomada.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any
from typing import cast
from uuid import uuid4

from app.graph.state import WorkflowBudget, WorkflowPhase
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import WorkflowAccessContext
from app.models.task import TaskStatus

logger = logging.getLogger("agent_forge")


class WorkflowNotFound(LookupError):
    pass


class NoPendingDecision(ValueError):
    pass


class WorkflowLeaseLost(RuntimeError):
    pass


class WorkflowCancelled(RuntimeError):
    pass


class WorkflowAlreadyTerminal(ValueError):
    pass


class WorkflowService:
    def __init__(
        self,
        graph_app: Any,
        settings: Settings,
        job_queue: Any,
        run_workers: bool,
        event_publisher: Any | None = None,
    ):
        self._app = graph_app
        self._settings = settings
        self._job_queue = job_queue
        self._run_workers = run_workers
        self._workers: list[asyncio.Task[Any]] = []
        self._running: dict[str, str] = {}
        self._failures: dict[str, str] = {}
        self._worker_id_prefix = str(uuid4())
        self._event_publisher = event_publisher
        self._invocations: dict[str, asyncio.Task[Any]] = {}
        self._cancel_requested: set[str] = set()

    # ------------------------------------------------------------------
    def _config(self, workflow_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": workflow_id}}

    def _ensure_workers_started(self) -> None:
        if self._workers or not self._run_workers:
            return
        for index in range(self._settings.workflow_worker_concurrency):
            task = asyncio.create_task(self._worker_loop(index))
            self._workers.append(task)

    def start_workers(self) -> None:
        self._ensure_workers_started()

    async def _mark_failed(self, workflow_id: str, exc: Exception) -> None:
        error = type(exc).__name__
        self._failures[workflow_id] = error
        logger.exception("Workflow %s falhou", workflow_id)
        try:
            await self._app.aupdate_state(
                self._config(workflow_id),
                {
                    "phase": WorkflowPhase.FAILED,
                    "final_output": "Workflow falhou durante a execução.",
                    "error": error,
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Não foi possível persistir a falha do workflow %s",
                workflow_id,
            )

    async def _worker_loop(self, index: int) -> None:
        worker_id = f"{self._worker_id_prefix}:worker-{index}"
        while True:
            failed = False
            await self._job_queue.touch_worker(worker_id)
            job = await self._job_queue.dequeue(
                worker_id=worker_id,
                poll_interval_seconds=self._settings.workflow_queue_poll_interval_seconds,
            )
            if job is None:
                continue
            self._running[job.workflow_id] = f"worker-{index}"
            try:
                await self._run_job_with_heartbeat(job)
            except asyncio.CancelledError:
                raise
            except WorkflowLeaseLost:
                failed = True
                logger.warning(
                    "Worker perdeu o lease do workflow %s; execução cancelada.",
                    job.workflow_id,
                )
            except WorkflowCancelled:
                failed = True
                await self._publish_event(
                    "workflow.cancelled", {"workflow_id": job.workflow_id}
                )
            except Exception as exc:  # noqa: BLE001
                failed = True
                await self._mark_failed(job.workflow_id, exc)
                await self._job_queue.fail(job, type(exc).__name__)
                await self._publish_event(
                    "workflow.failed",
                    {"workflow_id": job.workflow_id, "error": type(exc).__name__},
                )
            else:
                self._failures.pop(job.workflow_id, None)
                await self._publish_event(
                    "workflow.processed", {"workflow_id": job.workflow_id}
                )
            finally:
                self._running.pop(job.workflow_id, None)
                if not failed:
                    await self._job_queue.acknowledge(job)

    async def _invoke_job(self, job: Any) -> None:
        from langgraph.types import Command

        if job.kind == "start":
            await self._app.ainvoke(job.payload, self._config(job.workflow_id))
        else:
            await self._app.ainvoke(
                Command(resume=job.payload), self._config(job.workflow_id)
            )

    async def _maintain_lease(self, job: Any) -> None:
        interval = max(self._settings.workflow_queue_lease_seconds / 3, 0.01)
        while True:
            await asyncio.sleep(interval)
            if not await self._job_queue.heartbeat(job):
                raise WorkflowLeaseLost(job.workflow_id)

    async def _run_job_with_heartbeat(self, job: Any) -> None:
        invocation = asyncio.create_task(self._invoke_job(job))
        self._invocations[job.workflow_id] = invocation
        heartbeat = asyncio.create_task(self._maintain_lease(job))
        try:
            done, _ = await asyncio.wait(
                {invocation, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if invocation in done:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
                try:
                    await invocation
                except asyncio.CancelledError as exc:
                    if job.workflow_id in self._cancel_requested:
                        raise WorkflowCancelled(job.workflow_id) from exc
                    raise
                return

            invocation.cancel()
            with suppress(asyncio.CancelledError):
                await invocation
            await heartbeat
        except asyncio.CancelledError:
            invocation.cancel()
            heartbeat.cancel()
            for task in (invocation, heartbeat):
                with suppress(asyncio.CancelledError):
                    await task
            raise
        finally:
            self._invocations.pop(job.workflow_id, None)
            self._cancel_requested.discard(job.workflow_id)

    async def shutdown(self) -> None:
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            with suppress(asyncio.CancelledError):
                await worker
        self._workers.clear()
        if self._event_publisher is not None:
            await self._event_publisher.close()

    async def _publish_event(self, event: str, payload: dict[str, Any]) -> None:
        if self._event_publisher is not None:
            await self._event_publisher.publish(event, payload)

    async def readiness(self) -> dict[str, Any]:
        queue_error = None
        try:
            queue_ready = await self._job_queue.ping()
        except Exception as exc:  # noqa: BLE001
            queue_ready = False
            queue_error = type(exc).__name__
        queue_stats = await self._job_queue.get_stats() if queue_ready else None
        expected_workers = (
            self._settings.workflow_worker_concurrency if self._run_workers else 0
        )
        embedded_worker_health = not self._run_workers or (
            len(self._workers) == expected_workers
            and all(not worker.done() for worker in self._workers)
        )
        external_worker_health = (
            self._run_workers
            or self._settings.workflow_queue_backend != "postgres"
            or (queue_stats is not None and queue_stats.active_workers > 0)
        )
        ready = queue_ready and embedded_worker_health and external_worker_health
        return {
            "ready": ready,
            "queue_ready": queue_ready,
            "queue_error": queue_error,
            "embedded_workers_enabled": self._run_workers,
            "embedded_workers_running": len(self._workers),
            "embedded_workers_expected": expected_workers,
            "embedded_worker_health": embedded_worker_health,
            "external_worker_health": external_worker_health,
            "registered_workers": (
                queue_stats.active_workers if queue_stats is not None else 0
            ),
        }

    async def metrics(self) -> dict[str, Any]:
        queue_stats = await self._job_queue.get_stats()
        return {
            "workers": {
                "embedded_workers_enabled": self._run_workers,
                "configured_concurrency": self._settings.workflow_worker_concurrency,
                "running": len(self._workers),
                "busy": len(self._running),
                "registered": queue_stats.active_workers,
            },
            "queue": queue_stats.to_dict(),
            "workflow_runtime": {
                "active_workflows": len(self._running),
                "cached_failures": len(self._failures),
            },
        }

    # ------------------------------------------------------------------
    async def start(
        self,
        project_id: str,
        request: str,
        budget: WorkflowBudget | None,
        owner_client_id: str,
    ) -> str:
        workflow_id = str(uuid4())
        initial = {
            "request": request,
            "project_id": project_id,
            "workflow_id": workflow_id,
            "owner_client_id": owner_client_id,
            "budget": budget
            or WorkflowBudget(
                max_tokens=self._settings.default_max_tokens,
                max_cost_usd=self._settings.default_max_cost_usd,
                max_iterations=self._settings.default_max_iterations,
                max_wall_clock_seconds=self._settings.default_max_wall_clock_seconds,
            ),
        }
        self._failures.pop(workflow_id, None)
        self._ensure_workers_started()
        await self._job_queue.enqueue(
            workflow_id=workflow_id,
            project_id=project_id,
            owner_client_id=owner_client_id,
            kind="start",
            payload=initial,
        )
        return workflow_id

    async def get_access_context(self, workflow_id: str) -> WorkflowAccessContext:
        snapshot = await self._app.aget_state(self._config(workflow_id))
        if snapshot.values:
            values = cast(dict[str, Any], snapshot.values)
            return WorkflowAccessContext(
                workflow_id=workflow_id,
                project_id=values["project_id"],
                owner_client_id=values["owner_client_id"],
            )
        access = cast(
            WorkflowAccessContext | None,
            await self._job_queue.get_access(workflow_id),
        )
        if access is None:
            raise WorkflowNotFound(workflow_id)
        return access

    # ------------------------------------------------------------------
    async def get(self, workflow_id: str) -> dict[str, Any]:
        snapshot = await self._app.aget_state(self._config(workflow_id))
        pending = cast(Any, await self._job_queue.get_state(workflow_id))
        if not snapshot.values:
            if pending is not None:
                return cast(dict[str, Any], pending.to_pending_response())
            raise WorkflowNotFound(workflow_id)

        values = cast(dict[str, Any], snapshot.values)
        if "phase" not in values:
            if pending is not None:
                return cast(dict[str, Any], pending.to_pending_response())
            values = {**values, "phase": WorkflowPhase.LOADING_CONTEXT}
        interrupts = [i.value for i in getattr(snapshot, "interrupts", ()) or ()]
        if not interrupts:  # fallback: interrupts pendurados nas tasks do snapshot
            for t in snapshot.tasks or ():
                interrupts.extend(i.value for i in (t.interrupts or ()))

        plan = values.get("plan", [])
        return {
            "workflow_id": workflow_id,
            # enums SOLTOS em canal (não aninhados em modelo Pydantic) voltam
            # do msgpack como str — normalizar aqui, uma única vez
            "phase": WorkflowPhase(values["phase"]),
            "iteration": values.get("iteration", 0),
            "usage": values.get("usage", {}),
            "tasks": [
                {
                    "id": str(t.id),
                    "title": t.title,
                    "capability": t.capability,
                    "status": t.status,
                    "attempts": t.attempt_count,
                }
                for t in plan
            ],
            "pending_decision": interrupts[0] if interrupts else None,
            "final_output": values.get("final_output"),
            "error": values.get("error") or self._failures.get(workflow_id),
        }

    async def get_details(self, workflow_id: str) -> dict[str, Any]:
        snapshot = await self._app.aget_state(self._config(workflow_id))
        if not snapshot.values:
            raise WorkflowNotFound(workflow_id)
        values = cast(dict[str, Any], snapshot.values)
        return {
            "workflow_id": workflow_id,
            "project_id": values.get("project_id"),
            "tasks": [task.model_dump(mode="json") for task in values.get("plan", [])],
            "evaluations": [
                evaluation.model_dump(mode="json")
                for evaluation in values.get("evaluations", [])
            ],
        }

    # ------------------------------------------------------------------
    async def decide(self, workflow_id: str, decision: str) -> None:
        status = await self.get(workflow_id)  # levanta WorkflowNotFound
        if status["pending_decision"] is None:
            raise NoPendingDecision(
                f"Workflow {workflow_id} não está aguardando decisão."
            )
        access = await self.get_access_context(workflow_id)
        self._failures.pop(workflow_id, None)
        self._ensure_workers_started()
        await self._job_queue.enqueue(
            workflow_id=workflow_id,
            project_id=access.project_id,
            owner_client_id=access.owner_client_id,
            kind="resume",
            payload=decision,
        )

    async def cancel(self, workflow_id: str) -> None:
        status = await self.get(workflow_id)
        phase = WorkflowPhase(status["phase"])
        if phase in {
            WorkflowPhase.COMPLETED,
            WorkflowPhase.FAILED,
            WorkflowPhase.CANCELLED,
        }:
            raise WorkflowAlreadyTerminal(workflow_id)

        await self._job_queue.cancel(workflow_id)
        invocation = self._invocations.get(workflow_id)
        if invocation is not None:
            self._cancel_requested.add(workflow_id)
            invocation.cancel()
            with suppress(asyncio.CancelledError):
                await invocation

        snapshot = await self._app.aget_state(self._config(workflow_id))
        if snapshot.values:
            values = cast(dict[str, Any], snapshot.values)
            now = datetime.now(timezone.utc)
            cancelled_tasks = [
                task.model_copy(
                    update={"status": TaskStatus.CANCELLED, "updated_at": now}
                )
                for task in values.get("plan", [])
                if task.status != TaskStatus.COMPLETED
            ]
            await self._app.aupdate_state(
                self._config(workflow_id),
                {
                    "phase": WorkflowPhase.CANCELLED,
                    "plan": cancelled_tasks,
                    "final_output": "Workflow cancelado pelo operador.",
                    "error": "WorkflowCancelled",
                },
            )
