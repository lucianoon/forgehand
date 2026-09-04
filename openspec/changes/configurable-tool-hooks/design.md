## Context

Planner, executor and judge share ToolLoop. The container also builds lease-bound agents, including escalated executors. Existing webhooks only notify workflow outcomes and do not gate tools. Configuration belongs to the operator, never the model or generated repository.

## Goals / Non-Goals

Goals: typed lifecycle rules, enforcement before side effects, suppression before results enter model history, sanitized correlated audit and bounded execution.

Non-goals: arbitrary scripts, dynamic plugin loading, MCP, lifecycle events outside exploration tools, rollback of completed tools, or replacements for sandbox/path/command policies.

## Decisions

- `TOOL_HOOKS_JSON` is a size-bounded list of immutable, validated rules. Matchers use case-sensitive tool and agent globs, not executable expressions or regex. Unique rule IDs and event/action combinations are checked at startup. Empty list preserves default behavior.
- Events are `pre_tool`, `post_tool`, `tool_error`. Actions are `audit`, `deny` (pre only), `suppress` (post only). Post rules can apply only above a configured output character count. Every matching rule is evaluated in order; restrictive decisions win. No action can override built-in restrictions.
- One shared dispatcher is explicitly injected into all agent loops. Audit is recorded for each lifecycle event while enabled, even without matching rules. Unknown tool names are sanitized. Records exclude arguments, output, prompts and model-provided call IDs. A generated run ID and ordinal correlate events.
- A ContextVar carries trusted workflow/project/client ownership only during service graph invocation. It is reset even on cancellation and inherited safely by concurrent branches. Agent name and task ID are added per loop invocation, never stored as mutable global agent state.
- Audit writes have a bounded timeout. Failure aborts the loop with a safe error; pre failure prevents execution, post failure prevents forwarding the output but cannot undo execution. Cancellation propagates unchanged.
- ToolLoop locally enforces remaining calls within batches and stops after one forced-final response if the provider still requests tools. Denied/unknown calls consume slots. This avoids using a prompt instruction as the enforcement boundary.

## Risks / Trade-offs

- Post suppression is not rollback → document pre-hook use for preventing actions.
- Hooks cover only exploration tools → document exclusions for grounding, file application, build and delivery.
- Audit outages reduce availability → intentional fail-closed behavior when configured; operator can remove hooks and restart explicitly.
- JSONL is single-node audit, not tamper-proof → retain existing production caveat.
- Existing tool-call trace can contain workspace content → hook records themselves contain only metadata; blocked/suppressed output never enters the trace or model history.

## Migration Plan

No schema migration. Deploy with empty rules, configure audit backend and test policies, then restart every worker with identical environment. Roll back by restoring `TOOL_HOOKS_JSON=[]` and restarting workers. No live config endpoint.

## Open Questions

None for this bounded phase. Session/context/MCP/skill integration remains a separate design.
