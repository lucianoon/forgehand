## Why

Incremental deliveries can currently reserve an attempt and enqueue a workflow despite a known missing worker or unusable build configuration. Operators need actionable checks before dispatch, without paying for a model call or changing a repository.

## What Changes

- Add an authenticated, read-only local preflight report for an existing delivery plan, including its revision.
- Check plan eligibility/context limits, factory policy, configured build profiles, GitHub credential presence, and queue/worker health with a bounded timeout.
- Re-run these checks on the server before starting an incremental delivery; blockers leave the plan and queue untouched.
- Show checks and corrective actions in the existing Studio, explicitly separating locally checked conditions from remote/runtime conditions not checked.

## Capabilities

### New Capabilities

- `delivery-execution-preflight`: Side-effect-free local execution diagnostics and mandatory server-side dispatch guard.

### Modified Capabilities

None. The incremental delivery change remains unarchived; this capability adds a precondition without replacing its revision, authorization or merge gates.

## Impact

Delivery API, factory orchestration helper, Studio controls, unit/JavaScript tests and operator documentation. No new dependency, schema migration, paid API call, GitHub mutation, Docker restart or application generation. Existing non-product workflow endpoints are unchanged.
