# Factory qualification

Factory mode remains **disabled by default**. Passing mechanical tests is not
evidence that an LLM can deliver four independently correct PRs in five cases.
An authorized paid pilot has now run against dedicated public fixtures. See
[the dated live report](factory-live-results-2026-09-03.md) for actual outcomes.

## Reproducible fixtures

`benchmarks/factory/fixtures` contains dependency-free Python and Node projects.
`profiles.json` pins their official images by multi-platform manifest digest;
there are no package downloads during validation. Operator-approved commands
perform build and test phases. Never replace a digest with a floating tag.

Generate a new local repository for every reset (existing directories are never
reset or deleted):

```sh
python -m app.evaluation.factory_fixtures --ecosystem python
python -m app.evaluation.factory_fixtures --ecosystem node
```

These commands print the local path and SHA. Expected commits:

- Python: `0c7366b6cf69b2f5fca6229c48928f52d9609cfe`
- Node: `bbb1d7dbff357e1075e978941f98ee5d02c56693`

An operator must approve and seed two **dedicated public fixture repositories**
from these generated repositories before live qualification. The commands above
do not create GitHub repositories, push, or delete remote artifacts. Public
fixtures keep the existing benchmark reproducible and inspectable. Authenticated
checkout is now supported; see [private repositories](private-repositories.md).
A private pilot needs its own explicitly approved fixture and credentials.

The five cases in `cases.json` cover discount repair, tag normalization, useful
regression tests, shared tax calculation, and executable currency documentation.
Changing a fixture requires explicitly regenerating and reviewing manifest SHAs.
Provisioning rejects a moved base before planning or model calls.

## Mechanical validation (no LLM charges)

Run the trusted verifier controls with local Python and Node.js, without Docker,
network calls or an LLM:

```sh
uv run pytest -q tests/integration/test_factory_verifier_controls.py
```

These controls copy the versioned fixtures, reject unchanged baselines and
plausible incorrect implementations, and accept the correct reference for each
case. They also check that successful early process exits lack completion
evidence and that verifier execution leaves the original fixtures and copied
production files unchanged. They are regression controls for the verifiers,
not an environment for executing untrusted generated code on the host.

The separate container integration suite requires the pinned images:

```sh
docker pull python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
docker pull node@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5
RUN_FACTORY_DOCKER_TESTS=1 \
FACTORY_DOCKER_PYTHON_TEST_IMAGE=python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea \
FACTORY_DOCKER_NODE_TEST_IMAGE=node@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 \
pytest -q
node --test tests/web/*.test.cjs
```

For Docker Desktop, set `FACTORY_DOCKER_SOCKET` to the operator-approved local
socket. Tests cover both actual fixture builds, denied egress, read-only root,
non-root identity, absent credentials, OOM termination, timeout, truncation,
ordered phases and cancellation. Postgres/Neo4j tests require their separate
opt-ins and services.

## Live run (explicit cost and remote side effects)

Configure the OpenAI or OpenRouter runtime with factory mode enabled only
for the pilot, profiles from `benchmarks/factory/profiles.json`, and an approver
credential scoped to project `factory-qualification`. Keep SCM and LLM secrets
in server environment, never in profiles or work orders.
For direct OpenAI, see [backend setup](openai.md); select
`LLM_PROVIDER_BACKEND=openai` and load `OPENAI_API_KEY` only into the server.

The runner requires `FORGEHAND_API_KEY` and `GITHUB_TOKEN` in its environment.
Use a GitHub credential limited to the approved fixture repositories.

```sh
python -m app.evaluation.factory_qualification --allow-live \
  --python-repository APPROVED_OWNER/python-fixture \
  --node-repository APPROVED_OWNER/node-fixture \
  --total-budget 5 --output reports/factory-qualification.json
```

For repeated runs after fixture PRs were merged, create a dedicated branch at
each ecosystem's original manifest SHA and pass `--base-ref qualification-baseline`
(also available as the manual workflow's `base_ref` input). This selects the
same branch name in both repositories; it never changes the pinned SHA. Keep
that branch fixed and leave generated benchmark PRs unmerged. Do not reset or
force-push the fixture's main branch. Reports retain both the base ref and SHA.

Failed workflows retain a safe provider error category, HTTP status or transport
error type where available, without exporting raw provider response text.

The runner first probes sandbox boundaries and verifies that fresh containers
observe host file edits between builds, then submits sequential work orders
through the API. It reserves each case's declared maximum against the remaining
total budget, monitors usage and cancels nonterminal cases on timeout or budget
exhaustion. Provider metering can overshoot within an in-flight model request;
use provider-side spending limits as an additional hard billing control.

## Independent publication evidence

Each ready PR is qualified against one published commit and the case's pinned
baseline. The runner does not inherit success from an earlier verification:

1. Verify that the PR is open and unmerged. Its head repository, branch and SHA
   must match the recorded delivery; its base repository, branch and SHA must
   match the approved fixture repository and pinned case baseline. A fork,
   retargeted PR, moved head or moved base does not satisfy this identity check.
2. Read CI for that exact head SHA. Both check runs and current commit-status
   contexts are paginated, up to 3,000 entries per inventory. Incomplete,
   malformed, changing or oversized inventories fail verification rather than
   being treated as green. Qualification requires at least one completed check
   or status with conclusion `success`; a collection containing only neutral or
   skipped checks cannot qualify a PR. This extra requirement belongs to the
   qualification gate, not a general ban on neutral or skipped CI results.
3. Clone the **published SHA** into a fresh workspace and require the pinned
   baseline to be its ancestor. Inventory changed paths from the immutable
   local `base..head` Git objects, with rename detection disabled so both the
   deleted source and added destination of a rename are checked. The inventory
   must be nonempty and every path must match the case's allowed scopes. GitHub's
   PR file listing is not the source of this scope decision.
4. Add the independent verifier only after the agent finishes, then run it in
   the pinned Docker profile. The agent's repository does not contain that
   verifier. A successful verification needs one successful test phase, exit
   code zero, complete output, successful cleanup and the final stdout line
   `FORGEHAND_VERIFIER_OK`. A preexisting file or symlink at the reserved verifier
   path is rejected. The marker distinguishes completed checks from accidental
   `SystemExit(0)` or `process.exit(0)` during an import. It is not an adversarial
   attestation: code running in the same process could forge the marker or alter
   the verifier's runtime. Discount-repair and tag-feature cases also run the
   submitted tests on the candidate and on a targeted production mutation. The
   candidate's tests must pass normally and detect the reintroduced defect;
   absent or irrelevant tests cannot satisfy the requested regression. Mutation
   subprocesses are bounded, and spawn errors, timeouts or interrupted processes
   do not count as a successful mutation check. Production files are restored
   after each mutation, including when verification fails.
5. Read CI again and revalidate the PR's full publication identity after the
   independent checks finish. Only then record the verified commit SHA and
   independent-check success. A head/base change, closed or merged PR, or a
   newly pending/failing CI result invalidates the qualification attempt.

JSON reports retain `changed_paths`, `verified_commit_sha`, `verifier_sha256`
and `verification_profile_digest` alongside case outcomes and metrics. The
verifier hash identifies the exact script bytes; the profile fingerprint
identifies the execution configuration, including its pinned image and command.
These fields make evidence comparable without implying that an older run used
the current verifier. JSON and Markdown reports also retain the remote
branch/PR inventory; remote artifacts are never deleted.

These reads are bounded observations, not an atomic GitHub transaction. A PR
can change after the final check. Any merge or deployment decision must verify
its own current publication and CI state.

The release gate requires exactly five distinct cases, at least four green PRs
with independent checks and path scope passing, a successful sandbox preflight,
zero observed isolation violations and zero unclassified technical failures.
API, timeout and sandbox failures are classified separately. This is evidence
for these bounded fixtures, not a general guarantee of isolation or capability.

`.github/workflows/factory-qualification.yml` has only `workflow_dispatch`, uses
the protected `factory-qualification` environment, requires explicit fixture
destinations and budget, and uploads reports rather than raw runtime logs.
Choose `llm_provider` explicitly. Configure the matching `OPENAI_API_KEY` or
`OPENROUTER_API_KEY`, plus `FACTORY_GITHUB_TOKEN`, as environment secrets.
It does not run on push, create fixture repositories, or merge PRs.

## Current result and limitations

The latest complete historical live run passed its then-current release gate
with **4/5 independently verified green PRs**, as recorded in
[the dated live report](factory-live-results-2026-09-03.md). The later targeted
Node recheck failed and does not change that complete-round result. The approved
pilot used `lucianoon/forgehand-fixture-python`,
`lucianoon/forgehand-fixture-node`, and a USD 5 aggregate budget.

The stricter publication, CI and verifier criteria described here have local
regression coverage but have **not been requalified in a new complete run with
a real LLM**. The historical 4/5 result must not be reinterpreted as passing
these new criteria, and local mechanical checks do not establish a new model
success rate. Factory mode remains disabled by default and opt-in for pilots.

On the local macOS Docker Desktop 4.89.0 / Engine 29.7.2 installation, Apple
Virtualization with VirtioFS reproducibly returned stale Node source after a
host edit. Switching only file sharing to gRPC FUSE and restarting resolved
the minimal regression and all five independent baseline/reference checks.
This is a local compatibility finding, not a claim about every VirtioFS host.
Keep `test_node_observes_updated_file_between_builds` enabled during Docker
qualification. The preflight also rejects stale file visibility before any
paid work starts. The default factory flag is unchanged.

The historical live workflow used in-memory queue/checkpoint storage on one
runner. The durable backend now has PostgreSQL tests that kill a worker process
with SIGKILL and resume its queued work in another process; see
[worker recovery](worker-recovery.md) for checkpoint behavior, approval identity,
deployment compatibility and limits. Those tests use deterministic graph nodes,
not paid model calls or SCM publication. An external effect performed before a
node checkpoint can still be repeated; this is not a general exactly-once
publication guarantee. A multi-host deployment additionally needs shared
POSIX-lock-capable workspace storage and an operational recovery policy.
No automatic enablement, merge, deployment, or remote cleanup is implemented.
