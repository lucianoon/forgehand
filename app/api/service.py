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
from contextlib import nullcontext, suppress
from datetime import datetime, timezone
from typing import Any
from typing import cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.graph.state import DeliveryConfig, WorkflowBudget, WorkflowPhase
from app.infrastructure.settings import Settings
from app.infrastructure.installation import installation_descriptor, local_installation_checks
from app.infrastructure.workflow_queue import WorkflowAccessContext
from app.models.factory import WorkOrder
from app.models.factory import WorkspaceLifecycle, WorkspaceRetention
from app.factory.lifecycle import WorkspaceBusy
from app.factory.workspace import LocalGitWorkspaceManager
from app.infrastructure.audit import build_audit_event
from app.models.task import TaskStatus
from app.providers.base import ProviderError

logger = logging.getLogger("forgehand")


def _dump_model(value: Any) -> dict[str, Any] | None:
    """Modelos Pydantic voltam do checkpoint como instância; de canais soltos
    podem voltar como dict. Normaliza para dict JSON ou None."""
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return cast(dict[str, Any], value.model_dump(mode="json"))
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _dump_enum(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _latest_build_validation(plan: list[Any]) -> dict[str, Any] | None:
    """Obtém a evidência mais recente sem depender do formato do checkpoint."""
    for task in reversed(plan):
        attempts = getattr(task, "attempts", None)
        if not isinstance(attempts, list):
            continue
        for attempt in reversed(attempts):
            report = getattr(attempt, "build_validation", None)
            dumped = _dump_model(report)
            if dumped is not None:
                return dumped
    return None


def _snapshot_interrupts(snapshot: Any) -> list[Any]:
    interrupts = list(getattr(snapshot, "interrupts", ()) or ())
    if not interrupts:
        for task in getattr(snapshot, "tasks", ()) or ():
            interrupts.extend(getattr(task, "interrupts", ()) or ())
    return interrupts


class _ResumePosition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    resume_count: int = Field(ge=0, strict=True)


class WorkflowNotFound(LookupError):
    pass


class NoPendingDecision(ValueError):
    pass


class WorkflowLeaseLost(RuntimeError):
    pass


class WorkflowCancelled(RuntimeError):
    pass


class WorkflowResumeUncertain(RuntimeError):
    """The pending approval cannot be bound safely to its intended gate."""


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
        tracer: Any | None = None,
        workspace_manager: LocalGitWorkspaceManager | None = None,
        build_runner: Any | None = None,
        audit_log: Any | None = None,
    ):
        self._app = graph_app
        self._settings = settings
        self._job_queue = job_queue
        self._run_workers = run_workers
        self._tracer = tracer
        self._workers: list[asyncio.Task[Any]] = []
        self._running: dict[str, str] = {}
        self._failures: dict[str, str] = {}
        self._worker_id_prefix = str(uuid4())
        self._event_publisher = event_publisher
        self._invocations: dict[str, asyncio.Task[Any]] = {}
        self._cancel_requested: set[str] = set()
        self._workspace_manager = workspace_manager
        self._build_runner = build_runner
        self._audit_log = audit_log
        self._reconciler: asyncio.Task[Any] | None = None
        self._refresh_installation()

    def _refresh_installation(self) -> dict[str, Any]:
        descriptor = installation_descriptor(self._settings)
        configure = getattr(self._job_queue, "configure_installation", None)
        if callable(configure):
            configure(descriptor["fingerprint"], required=descriptor["required"])
        return descriptor

    # ------------------------------------------------------------------
    def _config(self, workflow_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": workflow_id}}

    def _ensure_workers_started(self) -> None:
        if self._workers or not self._run_workers:
            return
        if self._workspace_manager is not None:
            self._reconciler = asyncio.create_task(self._reconcile_loop())
        for index in range(self._settings.workflow_worker_concurrency):
            task = asyncio.create_task(self._worker_loop(index))
            self._workers.append(task)

    def start_workers(self) -> None:
        self._ensure_workers_started()

    async def _mark_failed(self, workflow_id: str, exc: Exception) -> None:
        error = type(exc).__name__
        if isinstance(exc, ProviderError):
            if isinstance(exc.status_code, int) and 400 <= exc.status_code <= 599:
                error += f":HTTP{exc.status_code}"
            elif type(exc.__cause__).__name__ in {
                "ReadTimeout", "WriteTimeout", "ConnectTimeout", "PoolTimeout",
                "ConnectError", "ReadError", "WriteError", "RemoteProtocolError",
            }:
                error += f":{type(exc.__cause__).__name__}"
        self._failures[workflow_id] = error
        # Provider messages and chained tracebacks can contain request/response data.
        if isinstance(exc, ProviderError):
            logger.error("Workflow %s falhou: %s", workflow_id, error)
        else:
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
        except Exception as persistence_error:  # noqa: BLE001
            # A chained exception here still contains the original provider body.
            logger.error(
                "Não foi possível persistir a falha do workflow %s: %s",
                workflow_id,
                type(persistence_error).__name__,
            )

    async def _worker_loop(self, index: int) -> None:
        worker_id = f"{self._worker_id_prefix}:worker-{index}"
        while True:
            failed = False
            self._refresh_installation()
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
                # Shutdown interrupted execution. Leave the delivery leased so
                # another worker can reclaim it; never ACK unfinished work.
                failed = True
                raise
            except WorkflowLeaseLost:
                failed = True
                logger.warning(
                    "Worker perdeu o lease do workflow %s; execução cancelada.",
                    job.workflow_id,
                )
            except WorkflowResumeUncertain:
                failed = True
                # Preserve the checkpoint and its human gate. A fresh explicit
                # decision can enqueue a bound resume; never mark the graph failed.
                await self._job_queue.fail(job, "resume_decision_unbound")
                await self._publish_event(
                    "workflow.resume_blocked", {"workflow_id": job.workflow_id}
                )
                logger.warning("Resume requires a fresh decision: %s", job.workflow_id)
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

    async def _interrupt_positions(self, snapshot: Any) -> dict[str, _ResumePosition]:
        """Bind a gate to its task-local persisted resume ordinal, not just its ID.

        LangGraph reuses interrupt IDs (and checkpoint IDs) for sequential
        interrupts in a node. Its task-local __resume__ pending write records
        how many earlier values that node has consumed. Never export the values.
        """
        config = getattr(snapshot, "config", None) or {}
        checkpoint_id = (config.get("configurable") or {}).get("checkpoint_id")
        checkpointer = getattr(self._app, "checkpointer", None)
        if not isinstance(checkpoint_id, str) or not checkpoint_id or not checkpointer:
            raise WorkflowResumeUncertain("approval_position_unavailable")
        saved = await checkpointer.aget_tuple(config)
        if saved is None:
            raise WorkflowResumeUncertain("approval_position_unavailable")
        positions: dict[str, _ResumePosition] = {}
        for task in snapshot.tasks:
            if not task.interrupts:
                continue
            # Nested checkpoint namespaces need their own ordinal resolution.
            # Do not silently treat a child's gate as ordinal zero of its parent.
            if task.state is not None:
                raise WorkflowResumeUncertain("nested_approval_position_unavailable")
            resumes = [
                value for task_id, channel, value in saved.pending_writes or []
                if task_id == task.id and channel == "__resume__"
            ]
            if len(resumes) > 1 or (resumes and not isinstance(resumes[0], list)):
                raise WorkflowResumeUncertain("approval_position_unavailable")
            position = _ResumePosition(
                checkpoint_id=checkpoint_id, task_id=task.id,
                resume_count=len(resumes[0]) if resumes else 0,
            )
            for item in task.interrupts:
                if item.id in positions:
                    raise WorkflowResumeUncertain("approval_position_unavailable")
                positions[item.id] = position
        if set(positions) != {item.id for item in _snapshot_interrupts(snapshot)}:
            raise WorkflowResumeUncertain("approval_position_unavailable")
        return positions

    async def _job_invocation(self, job: Any) -> tuple[bool, Any]:
        from langgraph.types import Command

        config = self._config(job.workflow_id)
        if job.kind == "start":
            if job.attempt_count > 1:
                snapshot = await self._app.aget_state(config)
                if getattr(snapshot, "created_at", None) or snapshot.values:
                    # None continues checkpointed work; a new payload restarts it.
                    if _snapshot_interrupts(snapshot) or not snapshot.next:
                        return False, None
                    return True, None
            return True, job.payload

        payload = job.payload
        if isinstance(payload, dict):
            if (
                type(payload.get("resume_version")) is not int
                or payload["resume_version"] not in {1, 2}
                or not isinstance(payload.get("decision"), str)
                or not payload["decision"].strip()
            ):
                raise WorkflowResumeUncertain(job.workflow_id)
            ids = payload.get("interrupt_ids")
            if (
                not isinstance(ids, list)
                or not ids
                or not all(isinstance(i, str) and i for i in ids)
            ):
                raise WorkflowResumeUncertain(job.workflow_id)
            positions: dict[str, _ResumePosition] = {}
            if payload["resume_version"] == 2:
                raw_positions = payload.get("interrupt_positions")
                if not isinstance(raw_positions, dict) or set(raw_positions) != set(ids):
                    raise WorkflowResumeUncertain(job.workflow_id)
                try:
                    positions = {
                        key: _ResumePosition.model_validate(value)
                        for key, value in raw_positions.items()
                    }
                except ValidationError:
                    raise WorkflowResumeUncertain(job.workflow_id) from None
            snapshot = await self._app.aget_state(config)
            if not (getattr(snapshot, "created_at", None) or snapshot.values):
                raise WorkflowResumeUncertain(job.workflow_id)
            interrupts = _snapshot_interrupts(snapshot)
            pending_ids = {item.id for item in interrupts}
            matching = [identifier for identifier in ids if identifier in pending_ids]
            if matching:
                if payload["resume_version"] == 1:
                    if job.attempt_count > 1:
                        # ID-only envelopes cannot distinguish later gates in
                        # the same task. Preserve ambiguity for a fresh decision.
                        raise WorkflowResumeUncertain(job.workflow_id)
                else:
                    current = await self._interrupt_positions(snapshot)
                    unchanged: list[str] = []
                    consumed = False
                    for identifier in matching:
                        before, now = positions[identifier], current[identifier]
                        if (before.checkpoint_id, before.task_id) != (
                            now.checkpoint_id, now.task_id
                        ):
                            continue
                        if now.resume_count < before.resume_count:
                            raise WorkflowResumeUncertain(job.workflow_id)
                        if now.resume_count == before.resume_count:
                            unchanged.append(identifier)
                        else:
                            consumed = True
                    matching = unchanged
                    if not matching:
                        # Continue with already persisted values when necessary;
                        # never append the old decision to the next interrupt.
                        return consumed, None
                return True, Command(
                    resume={identifier: payload["decision"] for identifier in matching}
                )
            if interrupts or not snapshot.next:
                # The approval was consumed or the graph is now at a different
                # human gate. An old job must never approve that new decision.
                return False, None
            return True, None
        if not isinstance(payload, str) or not payload.strip():
            raise WorkflowResumeUncertain(job.workflow_id)
        if job.attempt_count > 1:
            snapshot = await self._app.aget_state(config)
            if (
                getattr(snapshot, "created_at", None) or snapshot.values
            ) and not _snapshot_interrupts(snapshot):
                return bool(snapshot.next), None
            # Old string jobs cannot identify which pending gate was approved.
            # Preserve state and require an explicit fresh decision.
            raise WorkflowResumeUncertain(job.workflow_id)
        return True, Command(resume=payload)

    async def _invoke_job(self, job: Any) -> None:
        # Span raiz do job: os spans de LLM do grafo aninham aqui via
        # contexto do OTel (contextvars sobrevivem ao fan-out paralelo).
        span = (
            self._tracer.span(
                "workflow",
                {
                    "forgehand.workflow_id": job.workflow_id,
                    "forgehand.project_id": getattr(job, "project_id", ""),
                    "forgehand.job_kind": job.kind,
                },
            )
            if self._tracer is not None
            else nullcontext()
        )
        manager = self._workspace_manager
        lock = manager.journal.exclusive(job.workflow_id) if manager else nullcontext()
        from app.agents.hooks import HookScope, tool_hook_scope

        with span, lock, tool_hook_scope(
            HookScope(
                workflow_id=job.workflow_id,
                project_id=job.project_id,
                client_id=job.owner_client_id,
            )
        ):
            should_invoke, graph_input = await self._job_invocation(job)
            if not should_invoke:
                return
            decision = (
                job.payload.get("decision")
                if isinstance(job.payload, dict) and job.payload.get("resume_version") in {1, 2}
                else job.payload
            )
            if manager and job.kind == "resume" and decision == "retry":
                retained = manager.journal.get(job.workflow_id)
                if retained and retained.state in {
                    WorkspaceLifecycle.RELEASED,
                    WorkspaceLifecycle.RELEASING,
                }:
                    raise NoPendingDecision(
                        "Workspace expirado; crie uma nova ordem de trabalho."
                    )
                if retained and retained.state == WorkspaceLifecycle.RETAINED:
                    await manager.reconstruct(retained)
                    manager.transition(
                        retained,
                        WorkspaceLifecycle.ACTIVE,
                        retention=WorkspaceRetention(),
                    )
            if manager and self._build_runner is not None:
                if not await self._build_runner.retry_cleanup(job.workflow_id):
                    raise WorkspaceBusy("sandbox_cleanup_pending")
            await self._app.ainvoke(graph_input, self._config(job.workflow_id))

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await self.reconcile_workspaces()
            except Exception:
                logger.exception("Workspace reconciliation failed")
            await asyncio.sleep(10)

    async def reconcile_workspaces(self) -> None:
        manager = self._workspace_manager
        if manager is None:
            return
        terminal = {
            WorkflowPhase.COMPLETED,
            WorkflowPhase.READY_FOR_HUMAN_REVIEW,
            WorkflowPhase.FAILED,
            WorkflowPhase.CANCELLED,
        }
        for lease in manager.journal.leases():
            if lease.state == WorkspaceLifecycle.RELEASED:
                continue
            queue_state = await self._job_queue.get_state(lease.workflow_id)
            if queue_state and queue_state.status in {"queued", "processing"}:
                continue
            snapshot = await self._app.aget_state(self._config(lease.workflow_id))
            values = snapshot.values or {}
            phase = WorkflowPhase(values.get("phase", WorkflowPhase.FAILED))
            paused = bool(getattr(snapshot, "interrupts", ()))
            if (
                phase not in terminal
                and not paused
                and not (queue_state and queue_state.status == "failed")
            ):
                continue
            try:
                with manager.journal.exclusive(lease.workflow_id):
                    current_job = await self._job_queue.get_state(lease.workflow_id)
                    if current_job and current_job.status in {"queued", "processing"}:
                        continue
                    if (
                        self._build_runner is not None
                        and not await self._build_runner.retry_cleanup(
                            lease.workflow_id
                        )
                    ):
                        manager.transition(
                            lease,
                            WorkspaceLifecycle.FAILED,
                            failure_reason="sandbox_cleanup_pending",
                        )
                        continue
                    if lease.retention.retain_until is None:
                        success = phase in {
                            WorkflowPhase.COMPLETED,
                            WorkflowPhase.READY_FOR_HUMAN_REVIEW,
                        }
                        ttl = (
                            self._settings.factory_success_retention_seconds
                            if success
                            else self._settings.factory_failure_retention_seconds
                        )
                        lease = manager.retain(lease, ttl, phase.value)
                    cleaned = await manager.cleanup(lease)
                    if values and not paused:
                        await self._app.aupdate_state(
                            self._config(lease.workflow_id), {"workspace": cleaned}
                        )
                    if self._audit_log is not None and cleaned.state != lease.state:
                        await self._audit_log.record(
                            build_audit_event(
                                action="workspace_lifecycle",
                                outcome=cleaned.state.value,
                                workflow_id=lease.workflow_id,
                                project_id=values.get("project_id"),
                                client_id=values.get("owner_client_id"),
                            )
                        )
            except WorkspaceBusy:
                continue

    async def _maintain_lease(self, job: Any) -> None:
        interval = max(self._settings.workflow_queue_lease_seconds / 3, 0.01)
        while True:
            await asyncio.sleep(interval)
            if not await self._job_queue.heartbeat(job):
                raise WorkflowLeaseLost(job.workflow_id)
            if job.locked_by is not None:
                await self._job_queue.touch_worker(job.locked_by)

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
        if self._reconciler is not None:
            self._reconciler.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconciler
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
        descriptor = self._refresh_installation()
        queue_error = None
        queue_stats = None
        workers = {"active": 0, "compatible": 0, "incompatible": 0, "legacy": 0}
        try:
            queue_ready = await self._job_queue.ping()
            if queue_ready:
                queue_stats = await self._job_queue.get_stats()
                if descriptor["required"]:
                    workers = await self._job_queue.installation_workers(descriptor["fingerprint"])
        except Exception as exc:  # noqa: BLE001
            queue_ready = False
            queue_error = type(exc).__name__
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
        installation_compatible = not descriptor["required"] or (
            descriptor["fingerprint"] is not None
            and workers["compatible"] >= self._settings.installation_expected_workers
        )
        ready = queue_ready and embedded_worker_health and external_worker_health and installation_compatible
        return {
            "ready": ready,
            "installation_compatible": installation_compatible,
            "compatible_workers": workers["compatible"],
            "incompatible_workers": workers["incompatible"],
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

    async def installation_diagnostics(self) -> dict[str, Any]:
        """Administrative, non-secret diagnostics; no model or SCM request."""
        descriptor = self._refresh_installation()
        readiness = await self.readiness()
        checks = await local_installation_checks(self._settings, descriptor)
        workers = {"active": 0, "compatible": 0, "incompatible": 0, "legacy": 0}
        jobs = {"incompatible": 0, "legacy_unbound": 0, "unconfigured": 0}
        if readiness["queue_ready"]:
            workers = await self._job_queue.installation_workers(descriptor["fingerprint"])
            if descriptor["required"]:
                jobs = await self._job_queue.installation_jobs(descriptor["fingerprint"])
        checks.append({"name": "queue", "status": "pass" if readiness["queue_ready"] else "fail", "code": "ok" if readiness["queue_ready"] else "queue_unavailable"})
        checks.append({"name": "workers", "status": "pass" if readiness["installation_compatible"] and readiness["external_worker_health"] else "fail", "code": "ok" if readiness["installation_compatible"] and readiness["external_worker_health"] else "compatible_workers_missing"})
        if jobs["incompatible"]:
            checks.append({"name": "pending_jobs", "status": "fail", "code": "jobs_require_reconciliation"})
        return {
            "schema_version": 1,
            "ready": readiness["ready"] and all(check["status"] != "fail" for check in checks),
            "factory_mode": self._settings.factory_mode_enabled,
            "revision": descriptor["revision"], "fingerprint": descriptor["fingerprint"],
            "checks": checks, "workers": workers, "jobs": jobs,
            "expected_workers": self._settings.installation_expected_workers,
            "configuration": descriptor["configuration"],
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
    async def dispatch_scope(self) -> str:
        return str(await self._job_queue.dispatch_scope())

    async def start(
        self,
        project_id: str,
        request: str,
        budget: WorkflowBudget | None,
        owner_client_id: str,
        delivery: DeliveryConfig | None = None,
        work_order: WorkOrder | None = None,
        workflow_id: str | None = None,
        expected_dispatch_scope: str | None = None,
    ) -> str:
        self._refresh_installation()
        workflow_id = workflow_id or str(uuid4())
        initial: dict[str, Any] = {
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
        if delivery is not None:
            initial["delivery"] = delivery
        if work_order is not None:
            initial["work_order"] = work_order
        admitted = await self._job_queue.enqueue_start(
            workflow_id=workflow_id,
            project_id=project_id,
            owner_client_id=owner_client_id,
            payload=initial,
            repository=work_order.repository.full_name if work_order else "",
            idempotency_key=work_order.idempotency_key if work_order else None,
            expected_dispatch_scope=expected_dispatch_scope,
        )
        self._ensure_workers_started()
        return str(admitted)

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
                response = cast(dict[str, Any], pending.to_pending_response())
                response["work_order"] = await self._job_queue.get_work_order(
                    workflow_id
                )
                return response
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
            "delivery": _dump_model(values.get("delivery_result")),
            "work_order": _dump_model(values.get("work_order")),
            "workspace": _dump_model(
                (
                    self._workspace_manager.journal.get(workflow_id)
                    if self._workspace_manager
                    else None
                )
                or values.get("workspace")
            ),
            "budget": _dump_model(values.get("budget")),
            "workspace_history": self._workspace_manager.journal.history(workflow_id)
            if self._workspace_manager
            else [],
            "build_strategy": _dump_model(values.get("build_strategy")),
            "factory_stage": _dump_enum(values.get("factory_stage")),
            "active_phase": self._workspace_manager.journal.phase(workflow_id)
            if self._workspace_manager
            else None,
            "phase_evidence": _latest_build_validation(plan),
        }

    async def get_details(self, workflow_id: str) -> dict[str, Any]:
        snapshot = await self._app.aget_state(self._config(workflow_id))
        if not snapshot.values:
            raise WorkflowNotFound(workflow_id)
        values = cast(dict[str, Any], snapshot.values)
        return {
            "workflow_id": workflow_id,
            "project_id": values.get("project_id"),
            "work_order": _dump_model(values.get("work_order")),
            "tasks": [task.model_dump(mode="json") for task in values.get("plan", [])],
            "evaluations": [
                evaluation.model_dump(mode="json")
                for evaluation in values.get("evaluations", [])
            ],
            "delivery": _dump_model(values.get("delivery_result")),
            "build_strategy": _dump_model(values.get("build_strategy")),
            "factory_stage": _dump_enum(values.get("factory_stage")),
            "phase_evidence": _latest_build_validation(values.get("plan", [])),
        }

    # ------------------------------------------------------------------
    async def decide(self, workflow_id: str, decision: str) -> None:
        self._refresh_installation()
        await self.get(workflow_id)  # levanta WorkflowNotFound
        snapshot = await self._app.aget_state(self._config(workflow_id))
        interrupts = _snapshot_interrupts(snapshot)
        if decision == "retry" and self._workspace_manager is not None:
            lease = self._workspace_manager.journal.get(workflow_id)
            if lease and lease.state in {
                WorkspaceLifecycle.RELEASED,
                WorkspaceLifecycle.RELEASING,
            }:
                raise NoPendingDecision(
                    "Workspace expirado; crie uma nova ordem de trabalho."
                )
        if not interrupts:
            raise NoPendingDecision(
                f"Workflow {workflow_id} não está aguardando decisão."
            )
        try:
            positions = await self._interrupt_positions(snapshot)
        except WorkflowResumeUncertain:
            raise NoPendingDecision(
                "Não foi possível identificar com segurança a posição da aprovação."
            ) from None
        access = await self.get_access_context(workflow_id)
        self._failures.pop(workflow_id, None)
        self._ensure_workers_started()
        await self._job_queue.enqueue(
            workflow_id=workflow_id,
            project_id=access.project_id,
            owner_client_id=access.owner_client_id,
            kind="resume",
            payload={
                "resume_version": 2,
                "decision": decision,
                "interrupt_ids": [item.id for item in interrupts],
                "interrupt_positions": {
                    key: value.model_dump() for key, value in positions.items()
                },
            },
        )

    async def cancel(self, workflow_id: str) -> None:
        status = await self.get(workflow_id)
        phase = WorkflowPhase(status["phase"])
        if phase in {
            WorkflowPhase.COMPLETED,
            WorkflowPhase.READY_FOR_HUMAN_REVIEW,
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
