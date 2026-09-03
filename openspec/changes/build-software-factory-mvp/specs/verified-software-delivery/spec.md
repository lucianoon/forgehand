## ADDED Requirements

### Requirement: End-to-end factory workflow
The system SHALL advance an accepted work order through provisioning, grounding, planning, implementation, objective validation, judging, publication, and CI observation as one checkpointed workflow.

#### Scenario: Work order succeeds
- **WHEN** all planned tasks and objective validations pass within budget
- **THEN** the workflow publishes one pull request and reaches `ready_for_human_review` with links to the order, diff, validation evidence, commit, and CI result

### Requirement: Atomic and idempotent publication
The system SHALL publish approved workspace changes as one commit on the workflow branch and SHALL reuse the workflow branch and pull request on retry.

#### Scenario: Publication is retried without content changes
- **WHEN** delivery repeats with a tree identical to the previously published tree
- **THEN** the system reuses the existing commit and pull request without creating another commit

### Requirement: CI repair loop
The system SHALL treat a failed required CI check as objective failure and SHALL return actionable annotations to the tasks responsible for published files.

#### Scenario: Required CI check fails
- **WHEN** a required check concludes unsuccessfully and repair iterations remain
- **THEN** the workflow reopens affected tasks, records check output and annotations, and performs another bounded implementation cycle

#### Scenario: Repair budget is exhausted
- **WHEN** required CI still fails after the configured iteration, time, or cost limit
- **THEN** the workflow enters a human decision gate with the latest commit, failures, attempted repairs, and consumed budget

### Requirement: Human merge boundary
The MVP MUST NOT merge a pull request automatically and SHALL clearly mark the delivery as awaiting authorized human review.

#### Scenario: Pull request and CI are green
- **WHEN** publication succeeds and all required checks pass
- **THEN** the workflow reaches `ready_for_human_review` without calling a merge API

### Requirement: Durable recovery
The system SHALL checkpoint the work order, workspace lease, task attempts, publication identity, CI state, and human decisions needed to resume without duplicating side effects.

#### Scenario: Worker restarts after publishing a commit
- **WHEN** another worker resumes the workflow before pull-request creation completes
- **THEN** it discovers or reuses the workflow branch and commit and creates at most one pull request

### Requirement: Observable progress
The API and dashboard SHALL expose factory stage, workspace state, selected build strategy, active command, task attempts, budget, delivery identity, CI state, and next required human action.

#### Scenario: Operator inspects a running factory job
- **WHEN** an authorized operator opens the workflow status
- **THEN** the system returns the latest checkpointed progress without exposing credentials or unrestricted command output
