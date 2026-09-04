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
fixtures are currently required because the workspace Git transport does not
provide an authenticated private-repository credential helper.

The five cases in `cases.json` cover discount repair, tag normalization, useful
regression tests, shared tax calculation, and executable currency documentation.
Changing a fixture requires explicitly regenerating and reviewing manifest SHAs.
Provisioning rejects a moved base before planning or model calls.

## Mechanical validation (no LLM charges)

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

For each ready PR it verifies the remote head, re-reads CI, inventories changed
paths (including rename sources), and clones the **published SHA** into a fresh
sandbox. Independent checks are added only after the agent finishes and are not
part of the repository supplied to the agent. JSON and Markdown reports include
metrics and remote branch/PR inventory. Remote artifacts are never deleted.

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

The local mechanical suite and Docker checks pass (505 Python tests, 3 external
service skips, and 4 dashboard tests on 2026-09-03). The operator approved
`lucianoon/forgehand-fixture-python`, `lucianoon/forgehand-fixture-node`, and a
USD 5 aggregate pilot budget. Real model calls and fixture PRs are recorded in
the dated report. The release gate has **not** passed; factory mode remains opt-in.

On the local macOS Docker Desktop 4.89.0 / Engine 29.7.2 installation, Apple
Virtualization with VirtioFS reproducibly returned stale Node source after a
host edit. Switching only file sharing to gRPC FUSE and restarting resolved
the minimal regression and all five independent baseline/reference checks.
This is a local compatibility finding, not a claim about every VirtioFS host.
Keep `test_node_observes_updated_file_between_builds` enabled during Docker
qualification. The preflight also rejects stale file visibility before any
paid work starts. The default factory flag is unchanged.

The live workflow uses in-memory queue/checkpoint storage on one runner. Local
tests cover serialized checkpoint resume in a rebuilt graph; Postgres CI tests
cover the existing durable backend. A multi-host deployment additionally needs
shared POSIX-lock-capable workspace storage and an operational recovery policy.
No automatic enablement, merge, deployment, or remote cleanup is implemented.
