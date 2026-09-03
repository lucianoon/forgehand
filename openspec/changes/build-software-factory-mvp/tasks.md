## 1. Factory models and configuration

- [x] 1.1 Add typed `WorkOrder`, source snapshot, repository target, build-profile selection, and delivery-policy models with backward-compatible serialization tests.
- [x] 1.2 Add typed `WorkspaceLease` and lifecycle-state models containing workflow ownership, local path, branch, pinned base SHA, timestamps, and retention metadata.
- [x] 1.3 Extend workflow state and task-attempt summaries with work-order, workspace, build-strategy, and factory-stage fields.
- [x] 1.4 Add `FACTORY_MODE_ENABLED`, approved SCM hosts, workspace root, retention TTLs, sandbox defaults, and build-profile settings with secure production validation.
- [x] 1.5 Add migration and checkpoint compatibility tests proving legacy workflows still deserialize and execute with factory mode disabled.

## 2. Work-order intake

- [x] 2.1 Extend workflow creation schemas to accept either the legacy request or a factory work-order payload without changing existing API behavior.
- [x] 2.2 Implement canonical normalization and validation for direct work orders before queueing or LLM invocation.
- [x] 2.3 Extend the GitHub client with an installation-scoped, read-only issue fetch that returns the immutable source snapshot without leaking credentials.
- [x] 2.4 Implement GitHub issue URL parsing and allowlisted-host validation with SSRF, malformed URL, inaccessible repository, and cross-installation tests.
- [x] 2.5 Add principal-and-repository-scoped idempotency storage and API tests showing retries return the original workflow.
- [x] 2.6 Emit audit events and expose provenance for successful and rejected work-order intake.

## 3. Repository workspace provisioning

- [x] 3.1 Define the `WorkspaceManager` protocol and runtime factory interfaces for grounding, agent tools, file application, and validation.
- [x] 3.2 Implement a safe Git command runner that uses argument arrays, redacts credentials, bounds output and time, and rejects repositories outside configured hosts.
- [x] 3.3 Implement a read-only repository cache with per-repository locking and deterministic fetch of an authorized base ref.
- [x] 3.4 Implement creation of a workflow-exclusive checkout and `forgehand/<workflow-id>` branch pinned to the resolved base SHA.
- [ ] 3.5 Bind repository grounding, planner tools, executor tools, judge tools, Git snapshots, and workspace runtime to the active lease instead of global roots.
- [ ] 3.6 Add `provision_workspace` and failure routing to the graph before repository grounding and planning.
- [x] 3.7 Implement idempotent lease reconstruction or deterministic reprovisioning after worker restart.
- [x] 3.8 Add concurrency tests proving two workflows on one repository cannot observe or modify each other's writable workspace.

## 4. Workspace lifecycle and cancellation

- [ ] 4.1 Persist workspace lifecycle transitions and expose them through workflow status and audit events.
- [ ] 4.2 Track active local processes and Docker containers by workflow so cancellation terminates execution before releasing the lease.
- [ ] 4.3 Implement idempotent cleanup for completed, failed, expired, and partially provisioned workspaces without deleting remote branches or pull requests.
- [ ] 4.4 Add a reconciler for abandoned leases and tests for restart, repeated cleanup, TTL retention, and cancellation during validation.

## 5. Project build strategies

- [ ] 5.1 Define typed build profiles and named `prepare`, `build`, `test`, `lint`, and `types` phases backed only by operator-approved commands.
- [ ] 5.2 Implement strategy selection precedence for explicit profile, managed repository mapping, safe Python/Node detection, and `unsupported_build_strategy`.
- [ ] 5.3 Extend `CommandPolicy` to validate the complete executable, argument, environment, and working-directory contract of a selected phase.
- [ ] 5.4 Add Python and Node fixture profiles with deterministic images or dependency caches and document how operators add profiles safely.
- [ ] 5.5 Persist the chosen strategy and selection reason before executing repository code and expose both in API and audit output.
- [ ] 5.6 Add policy tests proving agent output and repository files cannot introduce commands, shell metacharacters, environment secrets, or paths outside the lease.

## 6. Sandboxed build and validation

- [ ] 6.1 Make Docker sandbox execution the required default for factory workflows while preserving explicit local execution for legacy development.
- [ ] 6.2 Enforce network-off, read-only root filesystem, writable leased workspace, dropped capabilities, no-new-privileges, process, CPU, memory, and timeout limits.
- [ ] 6.3 Separate dependency preparation from validation and require an explicit network policy without mounting SCM or LLM credentials.
- [ ] 6.4 Execute selected build phases in order and return typed success, command failure, policy rejection, timeout, resource-limit, cancellation, and infrastructure outcomes.
- [ ] 6.5 Sanitize and attach phase evidence to task attempts, judge input, workflow status, audit events, and delivery summaries.
- [ ] 6.6 Feed failed phase evidence into the bounded executor autocorrection loop and prevent judge approval while required phases fail.
- [ ] 6.7 Add integration tests for network denial, resource termination, credential absence, output truncation, phase ordering, and successful Python and Node builds.

## 7. Verified delivery workflow

- [ ] 7.1 Route factory workflows from approved local validation to the existing atomic GitHub delivery service using the lease branch and pinned base.
- [ ] 7.2 Make branch, commit, and pull-request publication idempotent across retry and worker restart, including a crash between commit and PR creation.
- [ ] 7.3 Map failed required CI checks and annotations back to tasks and files, then reopen only responsible tasks while repair budget remains.
- [ ] 7.4 Add the terminal `ready_for_human_review` state and ensure the MVP never invokes a GitHub merge operation.
- [ ] 7.5 Extend cancellation and human-gate behavior for unsupported strategy, dependency preparation, exhausted repair budget, and retained workspace decisions.
- [ ] 7.6 Add end-to-end tests covering green CI, red-CI repair, exhausted repairs, no-check repositories, cancellation, and durable resume without duplicated side effects.

## 8. Mission control

- [ ] 8.1 Add work-order creation controls for direct requests and GitHub issue URLs, including repository, base branch, profile, criteria, budget, and delivery settings.
- [ ] 8.2 Display provenance, pinned SHA, workspace state, selected strategy, active phase, phase evidence, attempts, budget, PR, CI, and next human action.
- [ ] 8.3 Add accessible states and actionable error messages for provisioning, unsupported strategy, policy rejection, sandbox failure, cancellation, and cleanup.
- [ ] 8.4 Add dashboard integration tests for submitting and observing a factory workflow without exposing credentials or unrestricted terminal output.

## 9. Factory qualification

- [ ] 9.1 Create versioned Python and Node fixture repositories with independent validation and clean-reset tooling.
- [ ] 9.2 Define at least five cases covering defect repair, feature addition, test addition, behavior-preserving refactor, and executable documentation or configuration change.
- [ ] 9.3 Extend the benchmark runner with base-SHA pinning, expected path scope, hidden-check results, PR/CI evidence, total budget, and remote artifact inventory.
- [ ] 9.4 Calculate green-PR rate, first-pass rate, intervention rate, technical-failure rate, isolation violations, tokens, cost, mean duration, and p95 duration.
- [ ] 9.5 Enforce the release gate of at least four green PRs in five cases, zero isolation violations, and zero unclassified technical failures.
- [ ] 9.6 Add a manually triggered GitHub Actions workflow that requires explicit secrets, never runs on push, uploads reports, and identifies created branches and PRs.
- [ ] 9.7 Run the complete local test suite and factory qualification, document results and known limitations, and keep factory mode disabled by default until the gate passes.
