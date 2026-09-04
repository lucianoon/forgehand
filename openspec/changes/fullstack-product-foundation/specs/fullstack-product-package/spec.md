## ADDED Requirements

### Requirement: Independent fullstack export
The studio SHALL export a versioned fullstack package only for an owned, ready product without another AI call or embedded credentials.

#### Scenario: Export existing product
- **WHEN** an authorized owner requests the fullstack download
- **THEN** the ZIP includes runtime, model, deployment files and operational instructions while the original frontend download remains available

### Requirement: Authenticated private persistent records
The generated application SHALL require revocable expiring sessions and isolate records by authenticated user, including search and export.

#### Scenario: Restart and another user
- **WHEN** a user saves a record and the application restarts
- **THEN** that user's record remains accessible and another user cannot read, update, delete or export it

### Requirement: Bounded validated and conflict-aware operations
The backend SHALL validate field types, reject unknown fields, bound payloads/pagination/export and require current versions for update and deletion.

#### Scenario: Stale update
- **WHEN** two clients edit the same version and one commits first
- **THEN** the other receives a conflict without overwriting the committed change

### Requirement: Browser authentication protections
The runtime SHALL hash passwords, store only session-token hashes, reject cross-origin mutations, throttle login attempts in shared storage and require secure cookies for production.

#### Scenario: Cross-origin request or revoked session
- **WHEN** an attacker submits a mutation from another origin or reuses a logged-out session
- **THEN** the operation is rejected and records are unchanged

### Requirement: Operable database-backed runtime
The runtime SHALL support PostgreSQL with bounded connection pooling, development-only SQLite, versioned migration, liveness and schema-aware readiness, and document backup/restore and limitations.

#### Scenario: Model drift
- **WHEN** the model changes after database initialization without an explicit migration
- **THEN** startup or readiness fails closed instead of silently reinterpreting existing records
