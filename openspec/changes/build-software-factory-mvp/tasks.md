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
- [x] 3.5 Bind repository grounding, planner tools, executor tools, judge tools, Git snapshots, and workspace runtime to the active lease instead of global roots.
- [x] 3.6 Add `provision_workspace` and failure routing to the graph before repository grounding and planning.
- [x] 3.7 Implement idempotent lease reconstruction or deterministic reprovisioning after worker restart.
- [x] 3.8 Add concurrency tests proving two workflows on one repository cannot observe or modify each other's writable workspace.

## 4. Workspace lifecycle and cancellation

- [x] 4.1 Persist workspace lifecycle transitions and expose them through workflow status and audit events.
- [x] 4.2 Track active local processes and Docker containers by workflow so cancellation terminates execution before releasing the lease.
- [x] 4.3 Implement idempotent cleanup for completed, failed, expired, and partially provisioned workspaces without deleting remote branches or pull requests.
- [x] 4.4 Add a reconciler for abandoned leases and tests for restart, repeated cleanup, TTL retention, and cancellation during validation.

## 5. Project build strategies

- [x] 5.1 Define typed build profiles and named `prepare`, `build`, `test`, `lint`, and `types` phases backed only by operator-approved commands.
- [x] 5.2 Implement strategy selection precedence for explicit profile, managed repository mapping, safe Python/Node detection, and `unsupported_build_strategy`.
- [x] 5.3 Extend `CommandPolicy` to validate the complete executable, argument, environment, and working-directory contract of a selected phase.
- [x] 5.4 Add Python and Node fixture profiles with deterministic images or dependency caches and document how operators add profiles safely.
- [x] 5.5 Persist the chosen strategy and selection reason before executing repository code and expose both in API and audit output.
- [x] 5.6 Add policy tests proving agent output and repository files cannot introduce commands, shell metacharacters, environment secrets, or paths outside the lease.

Progress note: profile configuration documentation is available in
`docs/factory-build-profiles.md`; executable, digest-pinned Python/Node fixtures
are in `benchmarks/factory`. Selection is checkpointed and exposed.
The DockerBuildRunner executes selected profiles and its reports now flow
through task attempts, the judge, workflow status, audit, and final summaries.

## 6. Sandboxed build and validation

- [x] 6.1 Make Docker sandbox execution the required default for factory workflows while preserving explicit local execution for legacy development.
- [x] 6.2 Enforce network-off, read-only root filesystem, writable leased workspace, dropped capabilities, no-new-privileges, process, CPU, memory, and timeout limits.
- [x] 6.3 Separate dependency preparation from validation and require an explicit network policy without mounting SCM or LLM credentials.
- [x] 6.4 Execute selected build phases in order and return typed success, command failure, policy rejection, timeout, resource-limit, cancellation, and infrastructure outcomes.
- [x] 6.5 Sanitize and attach phase evidence to task attempts, judge input, workflow status, audit events, and delivery summaries.
- [x] 6.6 Feed failed phase evidence into the bounded executor autocorrection loop and prevent judge approval while required phases fail.
- [x] 6.7 Add integration tests for network denial, resource termination, credential absence, output truncation, phase ordering, and successful Python and Node builds.

Validation note: unit tests cover the runner with a controlled Docker client.
Graph tests prove successful evidence propagation, audit callbacks, structural
judge veto, and a failed-test correction followed by a successful bounded retry.
Real Python/Node container tests ran successfully using the pinned official
images. Isolation, resource limits, cancellation, phase ordering and both fixture
profiles are covered. Lifecycle and container ownership are journaled outside
the checkout, with lease-aware reconciliation. See `docs/factory-lifecycle.md`.

## 7. Verified delivery workflow

- [x] 7.1 Route factory workflows from approved local validation to the existing atomic GitHub delivery service using the lease branch and pinned base.
- [x] 7.2 Make branch, commit, and pull-request publication idempotent across retry and worker restart, including a crash between commit and PR creation.
- [x] 7.3 Map failed required CI checks and annotations back to tasks and files, then reopen only responsible tasks while repair budget remains.
- [x] 7.4 Add the terminal `ready_for_human_review` state and ensure the MVP never invokes a GitHub merge operation.
- [x] 7.5 Extend cancellation and human-gate behavior for unsupported strategy, dependency preparation, exhausted repair budget, and retained workspace decisions.
- [x] 7.6 Add end-to-end tests covering green CI, red-CI repair, exhausted repairs, no-check repositories, cancellation, and durable resume without duplicated side effects.

Progress note: publication now binds the lease branch and pinned base, requires
complete successful phase reports, and reaches `ready_for_human_review` only
with an identified PR and green CI. The manual PR endpoint cannot bypass this
factory path. Tests cover moving-base isolation, unexpected remote heads, CI
gates, bounded red-CI repair, cancellation and explicit retry in a rebuilt graph
without agent reexecution. Publication intent, parents and tree enable recovery
after a lost commit/ref response; unknown branches remain refused. See
`docs/factory-delivery.md`. Live qualification has run; its gate failed at 2/5.

## 8. Mission control

- [x] 8.1 Add work-order creation controls for direct requests and GitHub issue URLs, including repository, base branch, profile, criteria, budget, and delivery settings.
- [x] 8.2 Display provenance, pinned SHA, workspace state, selected strategy, active phase, phase evidence, attempts, budget, PR, CI, and next human action.
- [x] 8.3 Add accessible states and actionable error messages for provisioning, unsupported strategy, policy rejection, sandbox failure, cancellation, and cleanup.
- [x] 8.4 Add dashboard integration tests for submitting and observing a factory workflow without exposing credentials or unrestricted terminal output.

## 9. Factory qualification

- [x] 9.1 Create versioned Python and Node fixture repositories with independent validation and clean-reset tooling.
- [x] 9.2 Define at least five cases covering defect repair, feature addition, test addition, behavior-preserving refactor, and executable documentation or configuration change.
- [x] 9.3 Extend the benchmark runner with base-SHA pinning, expected path scope, hidden-check results, PR/CI evidence, total budget, and remote artifact inventory.
- [x] 9.4 Calculate green-PR rate, first-pass rate, intervention rate, technical-failure rate, isolation violations, tokens, cost, mean duration, and p95 duration.
- [x] 9.5 Enforce the release gate of at least four green PRs in five cases, zero isolation violations, and zero unclassified technical failures.
- [x] 9.6 Add a manually triggered GitHub Actions workflow that requires explicit secrets, never runs on push, uploads reports, and identifies created branches and PRs.
- [x] 9.7 Run the complete local test suite and factory qualification, document results and known limitations, and keep factory mode disabled by default until the gate passes.

Qualification note (2026-09-03): the local suite passed with 505 tests and
3 external-infrastructure skips, including actual Docker isolation, fixture
and independent baseline/reference checks; all 4 dashboard behavior tests pass.
The stale-file regression was resolved locally by switching Docker Desktop
from VirtioFS to gRPC FUSE. See docs/factory-qualification.md for the scope
and the fail-closed preflight added before paid qualification.
The operator-approved live pilot completed all five cases after integration
fixes. Two PRs passed CI, hidden checks and scope verification, below the 4/5
release threshold. Aggregate recorded cost across attempts was USD 0.0621592;
this is not a provider-invoice reconciliation. See
`docs/factory-live-results-2026-09-03.md` for reports and remaining limitations.
9.7 is complete as an execution/documentation task, not as a release approval.
Factory mode remains disabled by default; no PR was merged.
