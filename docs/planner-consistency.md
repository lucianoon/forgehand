# Planner consistency before execution

Factory planners require a `write_paths` declaration for every proposed task.
Use exact repository-relative paths for files to create, edit or remove, or an
empty list for a read-only task. Legacy planners still accept an omitted/null
declaration. This is planning metadata, not an execution allowlist or permission
to mutate files; workspace, command, acceptance and publication gates remain
authoritative.

Before turning a proposal into executable tasks, the planner checks each task's
declared writes and explicit `file_created`/`file_modified` requirements against
its `file_unchanged` criteria. Paths are normalized so `./module.js` and
`module.js` cannot hide a conflict. Absolute paths, traversal and glob patterns
are rejected in this exact-path contract. Protection belongs to the individual
task; a later task may legitimately edit a file protected by an earlier task.

A contradiction returns feedback through the existing bounded plan-repair loop
(two attempts by default), before implementation starts. The validator does not
silently delete or downgrade criteria. Feedback asks the planner to preserve the
original request: preserving a function's behavior does not mean keeping its
containing file byte-for-byte unchanged. Persistent contradictions fail closed.

This addresses the Node feature pilot failure: the proposal required adding an
export to a module while using `file_unchanged` to preserve existing functions
in that same module. Regression tests cover repair, exhaustion, factory runtime
wiring, legacy compatibility, task-local protection and normalized paths.

## Incremental task acceptance

A task must be approvable before its dependent tasks can run. Prefer keeping a
small implementation change and its regression tests together. When they are
split, the planner must assign new coverage to the task that creates those
tests. The judge must still establish the current task's requested behavior,
but must not invent a new-test requirement for an implementation-only contract.
The factory still requires every dependent task to be approved with build
evidence before publication, and successful CI before human review.

`tests_pass`, `lint_pass` and `types_pass` prove successful execution of their
respective phase. They do not prove that a particular behavioral case is
covered. Behavioral and coverage requirements need separate, accurately typed
criteria; a label such as "empty input returns zero" must not use `tests_pass`
as its sole proof.

In factory mode the judge now reads the current attempt's sandbox report before
evaluating objective criteria. It maps `test`, `lint` and `types` separately,
without calling a model to reinterpret a successful test run. A missing phase,
missing current report, failed command or cleanup failure cannot pass. Reports
from earlier attempts and executor-written workspace feedback are not reused.
This fixes the Python live-run rejection where `tests_pass` fell back to the
model even though the runtime had already executed the test phase. Independent
acceptance, build and publication gates remain mandatory when configured.

## Limits

This is a consistency check over model declarations, not a proof of semantic
correctness. A model can omit an intended write or declare a read-only task
incorrectly; unrelated or contradictory natural-language requirements are not
fully understood by this check. Tests, objective criteria, independent fixture
checks and human review remain necessary. Failed planning calls without a
returned usage report also retain the existing billing-reconciliation limitation.
