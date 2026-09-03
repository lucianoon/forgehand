## ADDED Requirements

### Requirement: Canonical work order
The system SHALL normalize every factory request into a canonical work order containing source metadata, repository, base ref, requested outcome, acceptance criteria, budget, build profile, and delivery policy.

#### Scenario: Direct request is normalized
- **WHEN** an operator submits a direct factory request with a repository and acceptance criteria
- **THEN** the system creates a canonical work order and persists it with the workflow checkpoint

#### Scenario: Invalid request is rejected before provisioning
- **WHEN** a factory request omits the repository, requested outcome, or applicable budget limits
- **THEN** the system rejects it without creating a workspace or invoking an LLM

### Requirement: GitHub issue source
The system SHALL create a work order from an authorized GitHub issue and SHALL retain an immutable snapshot of the issue used for planning.

#### Scenario: Issue becomes a work order
- **WHEN** an operator submits an accessible GitHub issue URL
- **THEN** the system stores the issue URL, number, title, body, labels, repository, author identity, update timestamp, and retrieval timestamp in the work order

#### Scenario: Issue changes after intake
- **WHEN** a source issue changes after its work order has started
- **THEN** the running workflow continues with the stored snapshot and reports that snapshot in its provenance

### Requirement: Source authorization and scope
The system MUST verify that the authenticated principal can create workflows for the target repository and MUST restrict issue retrieval to the configured SCM host and installation scope.

#### Scenario: Repository is outside the installation
- **WHEN** a work order references a repository unavailable to the configured GitHub App installation
- **THEN** the system rejects the order without attempting anonymous fallback or another credential

### Requirement: Work-order idempotency
The system SHALL support a caller-provided idempotency key scoped to the principal and repository.

#### Scenario: Intake request is retried
- **WHEN** the same principal repeats a request with the same repository and idempotency key
- **THEN** the system returns the existing work order and does not create a second workflow
