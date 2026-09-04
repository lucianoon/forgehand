## Why

Workflow admission currently commits its idempotency claim before inserting a job. A crash can therefore produce a claim with no executable work, while incremental delivery keeps an uncertain attempt that requires manual investigation. Reliable recovery is the first step toward a professional, supervised delivery product.

## What Changes

- Atomically accept an idempotent workflow start and its queue job; retain an immutable admission receipt.
- Bind new incremental attempts to the queue's persistent identity and an exact saved submission so explicit recovery can safely repeat admission without creating a new workflow, budget, or attempt.
- Add an approver-only recovery endpoint, with revision/workflow confirmation, current execution preflight, and audit evidence. Reads and reconciliation never dispatch.
- Fail closed for legacy attempts, conflicting submissions, or a replaced queue. Document upgrade, retention and recovery limits.
- Non-goals: migrating all Studio data to PostgreSQL, automatic retries of paid effects, exactly-once execution of model/SCM calls, UI redesign, automatic merge/deploy.

## Capabilities

### New Capabilities

- `reliable-delivery-dispatch`: Atomic workflow admission and explicitly approved recovery of a persisted delivery intent.

### Modified Capabilities

None. Existing planning changes remain unarchived; this adds a separately bounded contract.

## Impact

Workflow queue/service, incremental ledger/service/API, additive PostgreSQL and SQLite schema, focused failure-injection/concurrency tests and operator documentation. No new provider or framework, no live AI calls, no remote repository mutations.
