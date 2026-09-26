## MODIFIED Requirements

### Requirement: Cloud session checkout

For `aws` and `gcp`, the system SHALL provision the writable Git checkout inside the isolator session from the pinned SHA and SHALL NOT use the host data root, NFS or another shared mount as that checkout. Isolation, cancellation and cleanup MUST still prevent concurrent workflows from sharing a writable tree. The `local` adapter keeps the existing host lease, lock and journal contract.

#### Scenario: Cloud adapter provisions a workspace
- **WHEN** a cloud execution-plane adapter claims a factory workflow
- **THEN** the writable checkout exists only in that isolator session and is destroyed on terminal state, cancellation or isolator timeout

#### Scenario: Local adapter unchanged
- **WHEN** `execution_plane` is omitted or `local`
- **THEN** workspace leases remain on the controller host filesystem with the current lock and retention rules
