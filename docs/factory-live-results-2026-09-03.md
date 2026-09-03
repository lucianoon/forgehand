# Live factory pilot — 2026-09-03

## Decision

**Release gate FAIL: 2/5 independently verified green PRs; 4/5 required.**
Factory mode remains disabled by default. This is evidence of two real, bounded
deliveries, not qualification as an autonomous general-purpose software factory.

The operator authorized two dedicated public fixture repositories and USD 5
aggregate LLM spending. No Forgehand implementation commit/push, PR merge,
production deployment, or GitHub secret upload was performed in this pilot.
Generated commits have one author and no coauthor trailer.

## Environment and scope

- Direct OpenAI: `gpt-4.1-mini-2025-04-14` for planning, execution and judging.
- Local API, worker, in-memory queue/checkpoints and one controller host.
- Docker Desktop 4.89.0 / Engine 29.7.2; gRPC FUSE; pinned Python/Node images.
- Controller uses an explicitly configured local Docker socket. Containers
  receive neither GitHub nor OpenAI credentials and have networking disabled.
- Python: [fixture repository](https://github.com/lucianoon/forgehand-fixture-python),
  base `0c7366b6cf69b2f5fca6229c48928f52d9609cfe`.
- Node: [fixture repository](https://github.com/lucianoon/forgehand-fixture-node),
  base `bbb1d7dbff357e1075e978941f98ee5d02c56693`.

Both main branches retain their seeded commits. These fixtures are test inputs,
not additional products or forks of the Forgehand application.

## Final five-case run

| Case | Result | Evidence / remaining issue |
| --- | --- | --- |
| Python defect | Human decision required; cancelled by harness | Generated code and local tests passed, but the planner embedded malformed content in a regex criterion; delivery was vetoed. |
| Node feature | Technical workflow failure | Failed before a persisted plan/usage result; detailed terminal provider metadata was not retained in the final list snapshot. |
| Python tests | PASS, first attempt | [PR #1](https://github.com/lucianoon/forgehand-fixture-python/pull/1), green CI, independent hidden check and path scope passed. |
| Node refactor | Human decision required; cancelled by harness | Planner treated API compatibility as `file_unchanged`, conflicting with refactoring; retries also encountered a stale search fragment. No PR published. |
| Python configuration | PASS, first attempt | [PR #2](https://github.com/lucianoon/forgehand-fixture-python/pull/2), green CI, independent executable-output check and path scope passed. |

Published commits are `161cd6195fb86b967d951afbad961baca4d00e84` (tests) and
`b03660828bb1e0fa53b0ff1b20b7b64b4705dbc6` (configuration). Both PRs remain open.
The verifier freshly cloned each published SHA; it did not validate only the
agent's working directory or rely only on the agent's judge.

Metrics: green-PR and first-pass rates 40%; intervention rate 40%; classified
technical-failure rate 20%. Sandbox preflight passed; zero observed isolation
violations and no unclassified technical failures were reported. This does not
establish a general security guarantee. The only failed release check was the
required four-green-PR threshold.

## Attempts, accounting and fixes

Four complete five-case attempts were retained, rather than discarding failures:

| Local report directory | Outcome | Recorded cost (USD) |
| --- | --- | ---: |
| `reports/forgehand-live.FKQ0t8` | Git provisioning failed before model calls | 0 |
| `reports/forgehand-live.9kzaPj` | OpenAI rejected the planner schema | 0 |
| `reports/forgehand-live.SaiLTV` | Planner ran; executor schema was rejected; no PRs | 0.0110408 |
| `reports/forgehand-live.oiGW2Y` | Final run above: two verified PRs | 0.0511184 |

Aggregate recorded usage: **198,448 tokens; USD 0.0621592**. This is application
metering, not a reconciled provider invoice or hard billing ceiling. Failed or
timed-out calls without usage can be absent from this total. Two minimal schema
probes returned HTTP 400 before generation; a model-access GET returned HTTP 200.
The final run's declared budget was reduced to USD 4.98 to reserve prior spend.

Implemented corrections, each covered locally:

1. Configurable, absolute local Docker socket, wired into the API worker.
2. Complete bare Git caches; materialize legacy blobless caches before local
   checkout. A filter-capable transport regression reproduced the missing-blob
   failure that ordinary local-path clones had concealed.
3. Remove Pydantic defaults from strict OpenAI schemas; the API had rejected a
   default beside a `$ref` in the real planner schema.
4. Normalize discriminated `oneOf` to `anyOf` for the real executor schema while
   preserving mutually exclusive operation tags and local Pydantic validation.

Validation after the code corrections: **505 Python tests passed, 3 external
service tests skipped**, including actual Docker integration; **4 dashboard
tests passed**. Ruff and Mypy passed (58 source files). No factory containers
remained after the final pilot, and its local API process was stopped.

## Remaining work before another release decision

1. Improve planning constraints: preserve behavior/API without requiring bytewise
   file immutability; do not encode an invented implementation as a content regex.
   Keep objective test and publication gates intact.
2. Retain safe provider error metadata and final details for every submitted ID.
   The workflow list derives its inventory from recent audit events, so repeated
   polling can hide older runs even though direct workflow access still exists.
3. Investigate transport timeouts and incomplete usage accounting on failed calls
   before scaling concurrency or running more expensive models.
4. Repeat all five cases after fixes; do not combine successes across different
   runs to claim 4/5. Keep human review mandatory and do not auto-merge.

Reports contain reserved branch names for every work order; a reserved name is
not proof that the branch was published. Only the two successful Python delivery
branches were observed remotely. Remote artifacts were preserved, not cleaned up.

## Follow-up after explicit merge approval

The operator subsequently authorized merging all reviewed changes. Python
fixture PRs #1 and #2 were merged as `42157816352bfd697795bb48f5d3c910e8e90172`
and `d47e7efafb7a044d54482f8cd50a5a72ef6717bd`. The result above describes the
earlier run, not the current fixture main branch. Do not reset main to rerun the
benchmark: provision a fresh approved fixture destination at the pinned seed.
The existing expected-SHA guard deliberately rejects a moved baseline.

Three technical regressions were reproduced and corrected after this run:

- HTTPX's default five-second timeout overrode the model-call budget. The
  compatible provider now passes the declared request timeout explicitly.
- Workflow history used rotating audit events as an index. It now lists the
  queue's workflow inventory, with owner/project filtering before pagination,
  retry deduplication and the same contract for memory and PostgreSQL.
- Executor criteria omitted the exact content regex required by the judge.
  Criteria formatting now includes it. Planner guidance also distinguishes
  behavioral/API compatibility from unchanged file bytes and discourages
  invented implementation-specific regex requirements.

These changes do not retroactively improve the 2/5 result. No additional paid
qualification was started in this follow-up. Safe provider-error detail retention
and failed-call billing reconciliation remain limitations. Factory mode stays
disabled by default, and the application still never merges autonomously.
