## Why

Passing tests and lint does not enforce module boundaries. Forgehand needs operator-approved architecture rules that produce deterministic, actionable feedback and veto publication when violated.

## What Changes

- Add bounded Python import-boundary policies to operator-controlled build profiles, included in profile fingerprints.
- Statically inspect Python imports without importing or executing repository code; report rule, file, line, dependency and remediation.
- Run checks before build phases and after successful phases, persist evidence, feed violations to the existing correction loop and require matching evidence before publication.
- Preserve behavior and fingerprints for profiles without architecture policies.

## Capabilities

### New Capabilities

- `architecture-policy-gates`: Deterministic Python dependency policies with build/publication veto and correction feedback.

### Modified Capabilities

None. This adds an optional gate to the existing factory build contract.

## Impact

Build/profile/evidence models, static checker, Docker build runner, selection/publication gates, graph feedback, tests and documentation. No remote writes, paid model calls, service restarts, UI redesign or autonomous policy editing. First release covers Python static imports, not arbitrary runtime dependency analysis or JavaScript/TypeScript.
