# Artifact evidence across retries

A task can be executed again after a checkpointed rejection, or corrected within
one executor call. Both paths pass the previous result to the workspace runtime.
The runtime reconciles only evidence belonging to that task, without replaying
previous operations or trusting the checkpoint's absolute workspace path.

The published file list contains the current bytes of all files changed by the
task. A retry that edits only a test retains the earlier implementation edit.
Deleted files stay deleted; creating then deleting a new file cancels the net
change. Deleting then recreating a file preserves its original baseline.

Current runtimes record `before_content` in each file diff to reconstruct the
change from the task's start. Older checkpoints are still readable: a `created`
entry establishes an absent baseline, but the original contents of a previously
modified file cannot be reconstructed when an old checkpoint did not record them.
Current bytes remain authoritative for publication and content criteria.

Validation results, application errors and Git snapshots are never inherited as
current evidence. A retry with no new operations still runs the configured checks
for the task's accumulated paths. Publication remains gated on the current build,
task judgments, dependent tasks and final acceptance.

Regression: `python -m pytest tests/unit/test_retry_evidence.py -q`.
