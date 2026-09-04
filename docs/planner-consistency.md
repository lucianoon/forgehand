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

## Limits

This is a consistency check over model declarations, not a proof of semantic
correctness. A model can omit an intended write or declare a read-only task
incorrectly; unrelated or contradictory natural-language requirements are not
fully understood by this check. Tests, objective criteria, independent fixture
checks and human review remain necessary. Failed planning calls without a
returned usage report also retain the existing billing-reconciliation limitation.
