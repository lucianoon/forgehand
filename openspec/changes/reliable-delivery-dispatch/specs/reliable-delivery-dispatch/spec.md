## ADDED Requirements

### Requirement: Atomic start admission

The queue SHALL atomically commit the idempotency mapping, immutable start receipt and executable start job. Matching repeats SHALL return one workflow without re-enqueuing, including after completion. Conflicting owner/project/content and legacy claims lacking receipts MUST fail closed.

#### Scenario: Failure before commit
- **WHEN** insertion of a start job fails after claiming its key
- **THEN** no key or receipt survives and a subsequent admission can enqueue exactly one start job

#### Scenario: Lost acknowledgement and concurrent callers
- **WHEN** admission commits but its acknowledgement is lost and concurrent callers repeat the same intent
- **THEN** they resolve to the same workflow and no additional start job is inserted

#### Scenario: Legacy or conflicting claim
- **WHEN** a key points to a claim without a receipt or a repeat changes the project or request
- **THEN** admission fails without dispatch or exposure of the original request

### Requirement: Bound and approved recovery

New delivery reservations SHALL atomically retain a queue namespace and saved-work-order digest. Recovery MUST require explicit approval, exact workflow identity, current revision, owner/project authorization and current execution preflight. Recovery SHALL use the existing immutable context, work order, base commit and limits without allocating another attempt.

#### Scenario: API restart before admission
- **WHEN** an approved attempt is saved but the API stops before queue admission and an approver requests recovery on the same durable queue
- **THEN** the saved workflow is admitted with the original context and budget

#### Scenario: Queue replacement or legacy intent
- **WHEN** the queue namespace has changed, intent is missing, or persisted content does not match its digest
- **THEN** recovery is blocked without starting work

#### Scenario: Unauthorized or stale recovery
- **WHEN** a viewer, another owner/project, stale revision, wrong workflow, missing approval or terminal attempt requests recovery
- **THEN** recovery is rejected and no job is created

#### Scenario: Read-only operations and current policy
- **WHEN** a plan is read or reconciled
- **THEN** it never dispatches, and a separate approved recovery is required with factory/runtime policies currently satisfied

### Requirement: Recovery evidence and operational limits

Recovery SHALL produce audit records and sanitized outcomes. Documentation MUST distinguish atomic admission from exactly-once execution, describe coordinated upgrades/backups and namespace rotation on restore, and state memory-backend and legacy limitations.

#### Scenario: Failure response
- **WHEN** recovery fails with an internal exception containing sensitive data
- **THEN** the attempt remains recoverable/uncertain and neither the API nor audit record exposes exception content
