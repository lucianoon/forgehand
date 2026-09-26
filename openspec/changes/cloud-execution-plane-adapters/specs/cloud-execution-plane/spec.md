## ADDED Requirements

### Requirement: Stable execution plane port

The system SHALL expose a single execution-plane port with claimer, isolator, identity and sink roles. Exactly one adapter (`local`, `aws`, or `gcp`) SHALL be active per installation. Mixing roles from different adapters MUST fail closed before any claim or sandbox starts.

#### Scenario: Explicit adapter selection
- **WHEN** the operator sets `execution_plane=local` on a team installation
- **THEN** workspace leases, Docker sandbox and POSIX locks follow the existing host contract and no cloud API is called

#### Scenario: Mixed adapter configuration
- **WHEN** identity is configured for AWS and the isolator is configured for GCP
- **THEN** startup and dispatch fail without claiming a job or creating a remote environment

### Requirement: Pull-based claim and durable control plane

Claimers SHALL obtain work only by outbound poll against the existing queue. The LangGraph loop, checkpoints and queue lease MUST remain on a durable control plane. Scheduled functions MAY wake a controller and MUST NOT run factory phases or the agent graph.

#### Scenario: Scheduled controller wakes
- **WHEN** EventBridge or Cloud Scheduler invokes the controller
- **THEN** the controller polls and claims over outbound HTTPS and, on success, starts one isolator session for that lease

#### Scenario: Function used as factory runner
- **WHEN** configuration points the isolator at AWS Lambda or a GCP Cloud Function
- **THEN** the adapter is rejected and no job is delivered to that runtime

### Requirement: Session-scoped isolator workspace

Cloud adapters SHALL clone the pinned SHA into a filesystem owned by that session and SHALL destroy the environment on terminal state, cancellation or isolator timeout. Control-plane metadata MUST survive. Shared host data roots, NFS and cross-host mounts MUST NOT be used as the writable checkout for `aws` or `gcp`.

#### Scenario: Two cloud sessions for one repository
- **WHEN** two workflows for the same repository are claimed by a cloud adapter
- **THEN** each session receives a distinct isolator filesystem and neither sees the other's writes

#### Scenario: Isolator finishes or times out
- **WHEN** the Fargate task or Cloud Run Job reaches a terminal state or its timeout
- **THEN** the writable checkout is gone, audit metadata remains, and an uncertain cleanup is quarantined rather than silently reused

### Requirement: Outbound-only perimeter and short-lived identity

Cloud isolators MUST initiate only outbound connections to the control plane, approved SCM hosts and configured model providers. They MUST obtain credentials by assuming an installation role or workload identity. Long-lived cloud keys MUST NOT be injected into the isolator environment. Secrets stay in the provider store until exchanged for a session credential.

#### Scenario: No inbound listener
- **WHEN** an `aws` or `gcp` isolator is started
- **THEN** it does not publish a public ingress and the control plane never opens an inbound path into the session

#### Scenario: Long-lived key offered
- **WHEN** configuration supplies a static AWS secret or GCP service-account JSON as isolator environment
- **THEN** the adapter refuses to start the session

### Requirement: Pinned images and network equivalent

Cloud isolators SHALL run operator-approved images pinned by digest. Tag-only pulls are rejected. Validation phases MUST keep the current no-network factory contract or the adapter MUST refuse the profile instead of weakening isolation.

#### Scenario: Profile image is digest-pinned
- **WHEN** a cloud isolator starts a factory phase
- **THEN** it runs the digest recorded in the approved profile and does not pull a moving tag

#### Scenario: Provider cannot disable phase network
- **WHEN** the selected cloud isolator cannot offer an equivalent to the local no-network sandbox for a validation phase
- **THEN** dispatch fails closed and the profile is not executed

### Requirement: Local adapter compatibility

The `local` adapter SHALL preserve the current single-host lifecycle: Docker socket, lease root, POSIX lock, ownership token and quarantine. Existing team Compose installations without `execution_plane` MUST keep that behavior.

#### Scenario: Default installation
- **WHEN** an installation omits `execution_plane`
- **THEN** it behaves as `local` and factory qualification still runs against the host Docker runner

### Requirement: Honest preflight for remote planes

Preflight SHALL keep current local checks for `local`. For `aws` and `gcp` it MUST mark isolator, identity and secret-store health as unverified until the adapter provides explicit attestation. In `prod`, missing attestation MUST block dispatch.

#### Scenario: Production without remote attestation
- **WHEN** `environment=prod` and `execution_plane` is `aws` or `gcp` and the adapter has not attested isolator, identity and secret store
- **THEN** start is rejected with a structured blocker and the queue is untouched

#### Scenario: Local health is not remote proof
- **WHEN** queue workers on the API host report ready while the cloud isolator is unattested
- **THEN** preflight does not treat that heartbeat as evidence that Fargate or Cloud Run can run the job

### Requirement: Sanitized evidence and operator limits

The sink SHALL keep logs and artifacts in the customer account, tagged with workflow id and ownership token. API responses and audit records MUST NOT include exception bodies, raw cloud credentials or isolator environment dumps. Documentation MUST state that tool execution residency is not model-inference residency, and that Lambda/Functions are controllers only.

#### Scenario: Isolator failure
- **WHEN** a cloud session fails with a provider error containing credentials or request bodies
- **THEN** the operator sees a stable error code and log reference, not the provider payload
