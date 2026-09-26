## MODIFIED Requirements

### Requirement: Remote execution-plane attestation

Preflight SHALL continue to report local queue, worker and profile checks. When `execution_plane` is `aws` or `gcp`, isolator, identity and secret-store health MUST be `unverified` until the active adapter attests them. In `prod`, missing attestation MUST block start without touching the queue. A ready heartbeat on the API host MUST NOT be treated as proof that the cloud isolator can run the job.

#### Scenario: Production cloud plane without attestation
- **WHEN** an approver requests start or preflight for a `prod` installation with `execution_plane=aws` or `gcp` and no isolator/identity/secret-store attestation
- **THEN** the report contains structured blockers, `can_start` is false, and no job is admitted

#### Scenario: Local worker ready, isolator unattested
- **WHEN** embedded or host workers report healthy and the cloud isolator has not attested
- **THEN** preflight lists isolator, identity and secret store as unverified and does not promote host heartbeat to remote proof
