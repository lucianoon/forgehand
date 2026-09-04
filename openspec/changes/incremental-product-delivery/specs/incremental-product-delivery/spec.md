## ADDED Requirements

### Requirement: Explicit durable delivery scope
The system SHALL persist an owner-scoped delivery plan for a ready Studio product with explicit target, features, criteria and preservation constraints. It SHALL preserve the original brief and prevent silent target or acceptance replacement.

#### Scenario: Restart and append
- **WHEN** a process restarts and an approver appends another feature
- **THEN** the prior requirements and receipts remain intact and the plan revision advances

### Requirement: Serialized approved dispatch
The system SHALL reserve exactly one active attempt per product using optimistic concurrency, persist its workflow ID and bounded immutable context before calling the factory, and require explicit approval/budget for each attempt.

#### Scenario: Concurrent clicks
- **WHEN** two clients start the same plan revision
- **THEN** at most one factory start is requested

#### Scenario: Uncertain dispatch
- **WHEN** enqueue success cannot be established or the worker state disappears
- **THEN** the original workflow ID remains visible and no new paid attempt is automatically submitted

### Requirement: Durable bounded context
The system SHALL include approved decisions, preservation constraints, current feature, original brief as historical context, prior verified receipts and pending work in a fingerprinted context capsule. It SHALL reject overflow rather than silently dropping requirements.

#### Scenario: Second delivery
- **WHEN** a previous increment has a verified merge receipt
- **THEN** the next workflow receives that receipt and the unchanged preservation requirements in its saved request

### Requirement: Evidence-based incorporation gate
The system SHALL distinguish running, human-review-ready, failed and merged increments. It SHALL require successful workflow CI plus read-only GitHub verification of the recorded PR/head, selected base and merge ancestry before advancing.

#### Scenario: Green unmerged PR
- **WHEN** CI passes but the PR is not merged
- **THEN** the next feature remains blocked

#### Scenario: Wrong head or rewritten history
- **WHEN** the PR head differs from the delivered commit or the base excludes its merge
- **THEN** incorporation is rejected and no next workflow is started

### Requirement: Authenticated visible workflow
The Studio SHALL expose preparation, append, explicit start, reconciliation and context inspection while preserving product owner/project access, approver-only mutations and factory-disabled gates.

#### Scenario: Unauthorized access
- **WHEN** another owner or a viewer attempts a mutation
- **THEN** no delivery state, queue item or remote mutation is created
