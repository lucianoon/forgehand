## ADDED Requirements

### Requirement: Read-only scoped execution report
The system SHALL provide an approver-only, owner/project-scoped local preflight report tied to the current product and revision, without starting workflows, changing plans, calling models, contacting SCM, or running repository code.

#### Scenario: Operator inspects a saved plan
- **WHEN** an authorized approver requests preflight
- **THEN** the report identifies blockers/warnings and the plan revision without changing its attempts or invoking SCM

#### Scenario: Unauthorized inspection
- **WHEN** a viewer or another owner requests preflight
- **THEN** access is denied before health or credential inspection

### Requirement: Conservative local checks
The system SHALL check delivery eligibility, attempt/context limits, enabled factory policy, approved host, Docker backend policy, credential presence, build configuration and queue/worker availability. Failed or timed-out health MUST block; errors MUST be sanitized.

#### Scenario: Missing worker or health timeout
- **WHEN** there is no supported live worker or health inspection fails or exceeds its timeout
- **THEN** preflight blocks with a stable actionable message and no raw exception body

#### Scenario: Auto-detected profile is not yet resolved
- **WHEN** an approved auto-detect candidate exists without explicit selection or repository mapping
- **THEN** preflight warns that selection still depends on checkout rather than claiming it was verified

### Requirement: Fresh mandatory dispatch guard
The start endpoint SHALL re-run local checks before reserving an attempt or contacting SCM, and return structured 409 on blockers. Existing revision, budget approval and merge verification MUST remain enforced.

#### Scenario: Configuration changes after a passing report
- **WHEN** preflight passed but workers become unavailable before start
- **THEN** start is rejected without modifying attempts or queueing work

### Requirement: Honest Studio diagnostics
The Studio SHALL render report messages as text, clear reports when plan or credential context changes, and disclose that GitHub permission, provider quota, Docker availability and remote worker configuration are not validated by local preflight.

#### Scenario: User checks without authorizing execution
- **WHEN** the user requests the checklist
- **THEN** only preflight is requested, the approval checkbox remains unselected, and no workflow starts
