## ADDED Requirements

### Requirement: Idea intake and approval
The system SHALL generate a bounded editable brief with audience, requirements, backlog and acceptance criteria from an idea, and MUST require explicit approval before generating application code.

#### Scenario: User approves a revised scope
- **WHEN** the owner submits an edited brief and approves construction
- **THEN** generation uses that exact brief and retains it with the resulting artifact

### Requirement: Durable authorized operations
The system MUST scope project records to the authenticated owner and authorized project, persist them across restarts, and prevent duplicate paid operations for retries of the same creation or approval.

#### Scenario: Duplicate request or another owner
- **WHEN** the same creation key is retried or another owner requests a record
- **THEN** the original operation is reused without another model call or access is denied respectively

### Requirement: Bounded generation
Generation SHALL use the configured provider router with bounded calls, time and output size, check estimated remaining budget before calls and retain usage or unknown-cost reservations on failure.

#### Scenario: Insufficient budget
- **WHEN** the next call estimate exceeds remaining budget
- **THEN** no model request starts and the project reports an actionable failure

### Requirement: Demonstrable application and export
The system SHALL produce a self-contained browser application with source download and manual acceptance checklist without requiring an existing repository.

#### Scenario: Code generation completes
- **WHEN** a structurally valid artifact is returned
- **THEN** the owner can preview it and download its code and approved brief, marked as ready for preview rather than production verified

### Requirement: Untrusted preview isolation
The system MUST NOT execute generated code on the controller, grant preview access to the parent origin, or expose credentials or unrestricted network access to the preview.

#### Scenario: Generated code attempts privileged access
- **WHEN** the preview attempts parent DOM access, network or top navigation
- **THEN** iframe sandbox and document CSP deny these capabilities
