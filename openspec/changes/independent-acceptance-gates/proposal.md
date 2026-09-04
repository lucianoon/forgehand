## Why

Repository tests can be rewritten by the same agent implementing a feature. A green build alone therefore cannot establish that approved requirements were met. Forgehand needs operator-owned, executable acceptance evidence checked outside candidate code.

## What Changes

- Add bounded operator-configured CLI input/output acceptance cases to build profiles, mapped explicitly to work-order criteria.
- Pin the suite and case identities before planning, reject uncovered criteria for acceptance-enabled profiles, and detect configuration drift.
- Run acceptance commands after ordinary phases in disposable network-disabled containers with read-only workspace; compare exact stdout on the host, not through candidate-authored assertions.
- Persist per-case results, veto failed/incomplete evidence in the graph and publication gate, and show coverage in review summaries.
- Preserve legacy profile fingerprints and clearly mark profiles without independent acceptance as unverified by this mechanism.
- Non-goals: generating business truth from model output, HTTP/browser/stateful acceptance, load/security certification, hidden-test secrecy guarantees, automatic merge or deploy.

## Capabilities

### New Capabilities

- `independent-acceptance`: Operator-owned black-box CLI acceptance with criterion coverage, pinned configuration and fail-closed publication evidence.

### Modified Capabilities

None; existing planning changes remain unarchived.

## Impact

Build profile/selection/result models, profile registry, Docker runner, graph feedback and publication checks, tests and operator documentation. No live AI calls or GitHub writes. Existing profiles remain compatible and do not claim independent verification.
