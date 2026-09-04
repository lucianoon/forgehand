## 1. Atomic queue admission

- [x] 1.1 Implement persistent queue identity, immutable admission receipts and atomic start insertion for memory/PostgreSQL; integrate WorkflowService and sanitized conflicts.
- [x] 1.2 Verify duplicate/concurrent starts, changed payload/project, legacy claims, rollback, lost acknowledgement and receipt retention.

## 2. Approved recovery

- [x] 2.1 Persist recovery metadata with delivery reservations and implement same-attempt recovery with immutable order and namespace checks.
- [x] 2.2 Add approved/revision-scoped recovery API, runtime preflight and audit; verify authorization, stale/legacy/terminal attempts and failure sanitization.

## 3. Validation and operations

- [x] 3.1 Exercise PostgreSQL persistence/transaction failures where local infrastructure permits and run Python/JavaScript regression, lint and type checks.
- [x] 3.2 Document activation, recovery procedure, coordinated backup/restore and remaining limitations; validate OpenSpec artifacts.
