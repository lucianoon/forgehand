## Context

The Studio uses single-host SQLite and a bounded declarative product model. The factory already creates isolated workspaces, validates changes and publishes human-reviewed PRs. This change connects them without claiming the demo is production-ready or expanding its original approval into unlimited work.

## Goals / Non-Goals

Goals: explicit repository selection and feature acceptance, one increment at a time, durable decisions/preservation requirements, immutable per-attempt context and GitHub-verified merge gates.

Non-goals: automatic repository creation/ZIP import, appointment-domain implementation, deployment, model-based context summarization, automatic merge, multi-host Studio or background reconciliation.

## Decisions

- A separate delivery-plan table in the Studio database snapshots the original brief as historical context. New explicit features and preservation constraints govern the evolution scope. Existing product payloads are unchanged.
- Plans are bounded to 20 features, 3 attempts per feature and bounded text. They have optimistic revisions; amendments only append future features and decisions, never remove previous acceptance criteria. Targets cannot change after creation.
- An append-only attempt table stores the exact context and work order plus SHA-256. The planner receives a bounded JSON capsule: original brief, approved decisions, preservation constraints, current feature, merged receipts and remaining feature titles. Raw model transcripts and secrets are not added. Context overflow is rejected, never silently truncated.
- SQLite BEGIN IMMEDIATE and compare-and-swap reserve one attempt before any dispatch. A server-generated workflow ID and idempotency key are persisted first, then passed to the existing service. If dispatch fails or the process dies, reads preserve the intent and reconciliation queries that exact workflow ID. Missing queue/checkpoint state is uncertain, not permission to resubmit. No automatic paid retry.
- Reconciliation is an explicit operator action. Failed/cancelled workflows may be retried by a new explicit approval; human gates remain in the workflow dashboard. Only ready-for-human-review with successful CI and a recorded PR/head can become merged.
- GitHub verification uses read-only PR, branch and compare endpoints. It requires merged=true, the exact recorded head SHA, expected repository/base branch and merge commit ancestry in the base. Before the next dispatch, the latest base is rechecked against the previous merge and pinned in the work order. Green CI or a client-supplied checkbox cannot substitute for this evidence.
- API mutations require approver role plus product owner/project access. Audit stores IDs and outcomes, not context contents. Factory mode remains opt-in. GitHub credential errors are redacted. No endpoint accepts arbitrary URLs, local paths or workflow IDs to attach.
- UI reuses Studio typography (Georgia headings, system sans body, monospace IDs) and palette: ink #17211b, muted #647069, panel #fffef8, green #147d4b, amber #b56b16. Signature: a sequential delivery ledger with state and evidence per feature, not another dashboard. Reviewable JSON allows precise criteria while avoiding a broad visual redesign.

## Risks / Trade-offs

- Cross-database dispatch cannot promise exactly-once execution → persist intent first and fail closed on uncertainty; document operator investigation rather than hiding it with retries.
- Saved context is bounded structured handoff, not semantic memory → never claim automatic long-context compaction or guaranteed correctness.
- A merge proves incorporation, not absence of defects → retain CI, human review and application-level tests.
- Repository permissions are server-credential permissions plus existing approver/project access → no claim of a new per-repository ACL.
- Existing/generated database migrations are not automatically proven safe → preservation requirements are passed to the factory and must be backed by tests for each real product.

## Migration Plan

CREATE TABLE IF NOT EXISTS adds isolated tables; existing Studio products remain readable. New routes/UI can prepare plans with factory disabled but cannot execute. No running services or remotes are changed during development. Back up the same Studio database. Rollback hides the controls and leaves additive tables intact.

## Open Questions

The operator must supply the real repository, base branch, build profile and feature acceptance before any live delivery. A pre-initialized repository is required; the existing ZIP can be imported separately with explicit authorization.
