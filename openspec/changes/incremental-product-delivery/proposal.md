## Why

Studio products stop at a downloadable demo/package, while factory workflows lose the product-level sequence between separate deliveries. Connect them through an operator-approved incremental backlog with durable, bounded context and verified incorporation of previous work.

## What Changes

- Add a persistent product delivery plan targeting an explicitly selected existing GitHub repository and branch.
- Dispatch one approved feature at a time through the existing factory, with an immutable context snapshot, fingerprint and per-run budget.
- Persist decisions, preservation constraints, attempts and receipts across process restarts; never treat model claims or a green PR as merged work.
- Verify the recorded PR head and its merge ancestry in the selected base before unlocking the next feature.
- Add append-only future features/decisions and a Studio delivery panel with explicit execution confirmation.
- Preserve safe uncertain-dispatch states rather than automatically repeating potentially paid or remote work.

## Capabilities

### New Capabilities

- `incremental-product-delivery`: Durable product backlog, context handoff, serialized factory dispatch, verified merge receipts and Studio controls.

### Modified Capabilities

None. No existing main specs are present.

## Impact

New delivery models/store/service/routes, a small internal workflow-ID injection point, SCM read-only merge verification, Studio controls and regression tests. Reuses ProductStore's SQLite file and existing factory settings/credentials.

## Non-goals

Creating GitHub repositories, importing the full-stack ZIP automatically, deployment, automatic merge, implementing the appointment domain itself, MCP, semantic compaction or multi-host Studio storage. These remain follow-up milestones. No live paid run is needed to implement and test this bridge.
