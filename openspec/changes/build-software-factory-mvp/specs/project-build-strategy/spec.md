## ADDED Requirements

### Requirement: Deterministic strategy selection
The system SHALL select a project build strategy using, in order, an explicitly requested profile, an operator-managed repository mapping, and safe file-based detection.

#### Scenario: Explicit profile is provided
- **WHEN** a work order names an allowed build profile
- **THEN** the system uses that profile and records the selection reason before executing project code

#### Scenario: No safe strategy matches
- **WHEN** no explicit, mapped, or safely detected strategy applies
- **THEN** the workflow enters an `unsupported_build_strategy` state without executing repository commands

### Requirement: Named build phases
Each build strategy SHALL define an ordered subset of `prepare`, `build`, `test`, `lint`, and `types` phases, with each phase mapped to an operator-approved command.

#### Scenario: Python validation strategy runs
- **WHEN** a Python strategy defines lint, types, and test phases
- **THEN** the system executes them in the configured order and records command identity, duration, exit code, truncated output, and resource outcome for every phase

### Requirement: Command policy enforcement
The system MUST reject commands, executables, arguments, environment variables, and working directories that violate the active command policy.

#### Scenario: Repository content proposes a new shell command
- **WHEN** an agent or repository file requests a command not present in the selected profile and allowlist
- **THEN** the system records a policy rejection and does not invoke a shell for that command

### Requirement: Sandboxed execution
Factory workflows MUST execute untrusted project phases in an isolated sandbox with network disabled by default and configured limits for time, CPU, memory, processes, and writable filesystem.

#### Scenario: Test attempts network access
- **WHEN** project tests attempt an outbound connection while network access is disabled
- **THEN** the connection fails without exposing host credentials or bypassing the sandbox

#### Scenario: Command exceeds a resource limit
- **WHEN** a build phase exceeds its configured time or resource limit
- **THEN** the sandbox terminates that phase and returns a typed limit failure to the workflow

### Requirement: Controlled dependency preparation
The system SHALL treat dependency preparation as a distinct, auditable phase and MUST NOT expose SCM or LLM credentials to it.

#### Scenario: Networked preparation is not authorized
- **WHEN** a strategy requires dependency downloads but the environment does not authorize networked preparation
- **THEN** the workflow pauses or fails with an actionable dependency-preparation reason before build phases run

### Requirement: Validation evidence
The system SHALL attach build-phase evidence to task attempts, judge input, workflow status, audit events, and delivery summaries without leaking configured secrets.

#### Scenario: Tests fail after a code change
- **WHEN** a test phase returns a non-zero exit code
- **THEN** the related task is not approved and receives the sanitized failure evidence for a bounded correction attempt
