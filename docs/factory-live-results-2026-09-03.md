# Live factory pilot — 2026-09-03

## Latest decision — repeatable-baseline run

**Release gate PASS: 4/5 independently verified green PRs in one complete run.**
The repeat run is recorded below; earlier failures remain intact as history.
Factory mode remains disabled by default. This qualifies the five bounded
fixtures, not autonomous production readiness or a general software capability.

## Earlier decision

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

## Repeatable-baseline run — 2026-09-03, 20:53 UTC

The operator asked to continue. Dedicated `qualification-baseline` branches were
created in the same two approved repositories at the original manifest SHAs.
Neither main branch was reset or modified. The runner now accepts `--base-ref`
and records the selected branch alongside the unchanged expected-SHA guard.
The manual qualification workflow exposes the same option.

Safe workflow diagnostics now preserve provider error categories, HTTP status
or transport exception types without serializing provider bodies. Regression
tests also cover chained exceptions when checkpoint persistence fails, and
ensure raw diagnostic text is excluded from exported qualification reports.

Report: `reports/forgehand-live.KmYmhp/qualification.json` (local, ignored).
All five cases used the same model, fixture SHAs and independent checks as the
previous run. The declared remaining budget was USD 4.93. No manual repair or
intervention was applied to any generated implementation during the run.

| Case | Result | Published evidence |
| --- | --- | --- |
| Python defect | PASS, first attempt | [PR #3](https://github.com/lucianoon/forgehand-fixture-python/pull/3), `d7a9be6df7de19bca752bc1ffb424a0c04f8e04a` |
| Node feature | Human decision required; harness cancelled | Planner incorrectly used `file_unchanged` for preserving individual price functions in the file receiving the new export; three attempts, no PR. |
| Python tests | PASS, first attempt | [PR #4](https://github.com/lucianoon/forgehand-fixture-python/pull/4), `db8b8930f27993b402b1b7de66e0ec33a0d52787` |
| Node refactor | PASS, first attempt | [PR #1](https://github.com/lucianoon/forgehand-fixture-node/pull/1), `ad8a2391124e873ecb2553adfd673fafec85709a` |
| Python configuration | PASS, first attempt | [PR #5](https://github.com/lucianoon/forgehand-fixture-python/pull/5), `85ebe5e4f874408eef6a8fb0d6e49c7be59c93f7` |

Each PASS includes successful GitHub CI, allowed-path verification and a fresh
clone of the published SHA passing independent hidden checks. All four PRs are
open against `qualification-baseline`; do not merge them into that pinned branch
if it will be reused for subsequent runs. The failed case's reserved branch
name in the inventory is not evidence of a remote publication.

Metrics: completion, first-pass and green-PR rates **80%**; intervention **20%**;
technical failures **0%**; zero observed isolation violations; all six release
checks passed. Recorded run usage: **162,648 tokens; USD 0.053436**. Aggregate
recorded pilot usage, including earlier failures: **361,096 tokens; USD 0.1155952**.
Metering is still not a reconciled provider invoice, and failed calls without
usage may be absent. The local pilot API was stopped and no factory containers
remained running after completion.

The remaining product issue is semantic plan consistency: prompt guidance alone
did not prevent the Node feature's contradictory file-immutability criterion.
Do not weaken publication checks or claim 5/5. A follow-up should validate plan
constraints against the requested edit before spending execution retries.
No additional Forgehand commit, push, merge or default enablement was performed
in this continuation.

Final local validation: **526 Python tests passed, 4 external-service tests
skipped**, including actual Docker execution; **4 dashboard tests passed**.
Ruff and Mypy passed (58 source files). The extra checkpoint-failure log
redaction was validated after the live run; it does not change case behavior.

## Targeted Node recheck after planner consistency validation

The next continuation added an explicit `write_paths` declaration to factory
plans and a task-local consistency check before execution. A deterministic
regression first reproduced acceptance of a plan that both edited and froze
`catalog.cjs`; the repaired planner rejects that proposal and uses its existing
bounded repair loop. It never silently removes acceptance criteria. See
`docs/planner-consistency.md` for the contract and its limits.

Only `node-feature` was rerun, with the same request, seed SHA, build profile,
hidden verifier, allowed paths and model. The per-case cap was USD 1, within the
original aggregate approval. Report: `reports/forgehand-node-recheck.zkFc7h/qualification.json`.
The new plan did not contain the contradictory file-immutability criterion.
The implementation task was approved, but the test-writing task exhausted three
attempts: replacement fragments were absent, a TAP output line was treated as
source, and the final generated tests used undefined `expect` assertions in a
Node assert-based fixture. The human gate was reached and the harness cancelled
the workflow. No new remote branch or PR was observed. This is a failed targeted
recheck, not 5/5 qualification; the latest complete-round result remains 4/5.

Recorded recheck usage: **62,920 tokens; USD 0.024058**. Aggregate recorded pilot
usage is now **424,016 tokens; USD 0.1396532**, subject to the same reconciliation
limitations above. The next unresolved area is reliable source-based test edits;
the observations do not by themselves prove that refreshing grounding alone
would fix every failure. Publication and objective checks were not bypassed.

Final validation of this continuation: **542 Python tests passed, 4 skipped**;
**4 dashboard tests passed**; Ruff, Mypy and strict OpenSpec validation passed.
An earlier Docker test attempt lacked this session's socket/network permissions;
the full suite above was rerun after access was granted. The pilot API was
stopped, no factory containers remained, and the reserved Node branch returned
HTTP 404. Changes remain local without commit, push, merge or default enablement.
