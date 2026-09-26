## 1. Execution plane contract

- [ ] 1.1 Define the `ExecutionPlane` port (`Claimer`, `Isolator`, `Identity`, `Sink`) and `execution_plane=local|aws|gcp` settings, failing closed on mixed-cloud role configuration.
- [ ] 1.2 Wrap the current host worker, Docker sandbox, POSIX lock and lifecycle journal as the `local` adapter without changing the default team Compose path.

## 2. Cloud adapters

- [ ] 2.1 Implement the AWS adapter contract: EventBridge-woken controller, Fargate session isolator, SSM/Secrets Manager, task-role identity, CloudWatch sink; reject Lambda as isolator and long-lived keys.
- [ ] 2.2 Implement the GCP adapter contract: Scheduler-woken controller, Cloud Run Job isolator, Secret Manager, Workload Identity, Cloud Logging sink; reject Cloud Functions as isolator and service-account JSON in the job environment.
- [ ] 2.3 Provision a session checkout from the pinned SHA inside the isolator; quarantine uncertain cleanup; never use NFS or the host data root as the writable cloud workspace.

## 3. Control plane and preflight

- [ ] 3.1 Keep queue lease, heartbeat, resume and human gates on the durable control plane; scheduled functions may only poll, claim and start one isolator session.
- [ ] 3.2 Extend delivery preflight so `aws`/`gcp` mark isolator, identity and secret store as unverified until attested, and block `prod` dispatch without attestation.

## 4. Validation and operations

- [ ] 4.1 Add contract tests with fake claimer/isolator/identity/sink covering mixed config, inbound/long-lived key rejection, session isolation, timeout quarantine and sanitized failures. No live AWS/GCP calls.
- [ ] 4.2 Version sketch IaC for the AWS and GCP mappings and document activation, rollback to `local`, log correlation and the limit that inference still leaves the customer account. Validate OpenSpec artifacts.
