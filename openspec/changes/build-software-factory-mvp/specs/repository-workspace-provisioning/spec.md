## ADDED Requirements

### Requirement: Isolated workspace per workflow
The system SHALL provision a writable Git workspace dedicated to one workflow and SHALL prevent concurrent workflows from sharing that writable checkout.

#### Scenario: Two workflows target one repository
- **WHEN** two factory workflows for the same repository execute concurrently
- **THEN** each workflow receives a distinct workspace and changes from either workflow are absent from the other

### Requirement: Pinned repository state
The system SHALL resolve the requested base ref to a commit SHA before planning and SHALL persist that SHA with the workspace lease.

#### Scenario: Base branch advances during execution
- **WHEN** the remote base branch receives new commits after provisioning
- **THEN** grounding, implementation, and validation continue against the pinned base SHA until an explicit rebase operation is requested

### Requirement: Safe repository acquisition
The system MUST acquire repositories only from configured SCM hosts and authorized repositories, without embedding credentials in URLs, logs, checkpoints, or agent context.

#### Scenario: Repository URL targets an unapproved host
- **WHEN** a work order resolves to an SCM host not present in the operator allowlist
- **THEN** provisioning fails before a network connection is attempted

### Requirement: Workflow-scoped runtime
The system SHALL bind grounding, agent tools, file operations, Git snapshots, and validation commands to the workspace lease of the active workflow.

#### Scenario: Worker resumes a workflow
- **WHEN** a worker resumes a checkpointed workflow with a valid workspace lease
- **THEN** it reconstructs all workspace-dependent runtimes from that lease before continuing

### Requirement: Workspace lifecycle
The system SHALL track workspace states and SHALL provide idempotent cleanup governed by outcome and retention policy.

#### Scenario: Successful delivery completes
- **WHEN** a workflow reaches `ready_for_human_review` and its success retention period expires
- **THEN** the system removes the local writable workspace while preserving audit metadata, the remote branch, and the pull request

#### Scenario: Failed workflow is retained
- **WHEN** a workflow fails and failure retention is enabled
- **THEN** the workspace remains available until its configured TTL and its path is visible only to authorized operators

### Requirement: Cancellation containment
The system MUST stop active workspace commands before releasing or cleaning a cancelled workflow lease.

#### Scenario: Workflow is cancelled during tests
- **WHEN** an operator cancels a workflow while a validation command is running
- **THEN** the command process or sandbox is terminated before cleanup begins and no subsequent task writes to that workspace
