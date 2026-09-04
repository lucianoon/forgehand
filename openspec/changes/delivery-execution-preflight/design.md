## Context

The product delivery API checks factory enablement and SCM ancestry, but can dispatch with no available worker or approved build candidate. Its boolean factory flag does not explain operational blockers. Existing queue readiness already provides health signals, and build policy is operator-controlled.

## Goals / Non-Goals

Goals: bounded read-only diagnostics, actionable stable check codes, mandatory fresh check before dispatch, and explicit disclosure of unverified conditions.

Non-goals: generating an application, executing a sample job, probing Docker, authenticating to GitHub/OpenAI, repairing configuration, changing non-product workflows, multi-host capability attestation or certifying production readiness.

## Decisions

- A typed preflight report contains product/revision, checks (pass/block/warning), a derived `can_start`, and unverified conditions. Reports are not persisted or authorization tokens.
- `GET /products/{id}/delivery/preflight` requires owner/project plus approver. It queries only local configuration and existing queue/worker health, with a two-second async timeout. Unknown or failed health blocks dispatch; raw error text and credentials never enter reports.
- Explicit build selection takes precedence over repository mapping, matching current selection policy. Unknown profiles or absence of any auto-detect candidate block. Auto-detection is a warning until checkout, not a claim of a selected profile. An explicitly selected profile requiring disallowed dependency network blocks.
- Start rechecks the report before SCM and before reserving any attempt; a blocker yields structured 409. Revision/CAS and actual SCM checks remain authoritative. A prior successful GET cannot bypass current checks.
- The Studio adds a compact textual checklist next to existing execution controls. Keep existing palette/type/layout, no new hero or demo. Reports clear on plan/identity changes. Unknown report state does not replace the server guard, and a user must still explicitly approve execution.

## Risks / Trade-offs

- Health can change after the snapshot → retain existing durable dispatch and uncertainty behavior; do not promise execution success.
- Worker heartbeat cannot attest to worker-local Docker or profiles → name these as unverified, particularly with external workers.
- GitHub token presence cannot establish permission; no paid call establishes provider quota → report presence only and explicitly list the unverified checks.
- In-memory queue with embedded workers disabled can report ready historically → explicitly block this unsupported execution combination in preflight.
- Profiles still depend on actual repository contents → do not clone or run code during the check.

## Migration Plan

Additive endpoint and UI, no storage migration. Existing delivery clients receive a structured 409 for configurations now blocked before dispatch. Rollback removes the new guard without changing plan data. Do not enable factory mode or alter user credentials as part of rollout.

## Open Questions

None for this bounded local preflight; remote capability attestation is future work.
