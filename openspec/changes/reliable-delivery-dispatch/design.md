## Context

The incremental ledger persists a work order before calling WorkflowService.start. Start separately claims idempotency and enqueues a job. Ledger and queue have different transaction domains. Queue jobs already survive restart with PostgreSQL, but claiming alone is not evidence of admission.

## Goals / Non-Goals

Goals: atomic admission, explicit replay of the same immutable intent, concurrency safety, fail-closed legacy handling, and executable fault tests.

Non-goals: moving the full Studio to PostgreSQL, background dispatch, automatic retry of terminal executions, remote exactly-once effects, changing workers/checkpoints, UI redesign.

## Decisions

1. Add queue `enqueue_start` that commits the idempotency mapping, immutable start receipt and job together. A repeat returns the original workflow only after checking owner/project and canonical request fingerprint. Ignore generated WorkOrder id/created_at and candidate workflow id in the fingerprint; preserve all operational fields. Existing split claims without a receipt fail closed, not silently succeed. Keep existing enqueue for resume/compatibility.
2. Keep start receipts after job completion/cancellation. Bind a receipt to workflow id as well as the scoped key, preventing duplicate starts even without a key. PostgreSQL uses one database transaction; the memory implementation serializes under its condition lock and pre-serializes before mutation.
3. Create a queue namespace UUID (persistent in PostgreSQL, per-instance in memory). Save it plus the work-order digest alongside each new delivery reservation in the same SQLite transaction. Reject recovery if the queue namespace differs. This prevents replay into a fresh in-memory queue or replacement database. Restoring an old snapshot is not detectable by UUID alone: require a coordinated restore and namespace rotation before admitting new recovery traffic.
4. POST recovery requires approver role, owned project/product, revision, explicit approval and the exact workflow id. Only dispatching/dispatch_unknown attempts with new-protocol intent qualify. Use saved work order, context, base SHA and original limits. Check current runtime policies without recalculating context or consuming an attempt. Namespace comparison is enforced inside queue admission too, not only as a pre-check.
5. Reads and reconciliation never dispatch. Recovery does not restart a completed/failed job: an existing matching receipt means no-op admission. Reconcile separately to read execution state. Record audit before admission and sanitized outcome; never return exception/provider bodies.

## Risks / Trade-offs

- Independent stores → persistent intent plus atomic idempotent admission closes the dispatch retry gap, not a distributed transaction or complete Studio migration.
- Worker redelivery/remote effects → explicitly no exactly-once model/SCM promise; existing leases and human review remain necessary.
- Receipt deletion or stale backup → retain mappings, receipts and queue identity together; pause traffic and rotate namespace on restore before investigation.
- Mixed server versions → quiesce admission and upgrade API/workers together; legacy uncertain attempts remain manual.
- Memory backend → recovery only within that queue instance; use PostgreSQL for restart durability.

## Migration Plan

Additive schema only, no existing rows removed or rewritten. Stop admission, back up SQLite and PostgreSQL together, upgrade API/workers and initialize schema, then resume traffic. Existing attempts reconcile as before but cannot use recovery. Rolling back requires stopping admissions and preserving new metadata; never drop receipts to retry work.

## Open Questions

None blocking this bounded milestone. Full PostgreSQL Studio migration and guided recovery UI remain subsequent work.
