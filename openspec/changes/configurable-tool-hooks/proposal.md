## Why

Forgehand has tool-use and hardcoded guardrails, but operators cannot configure lifecycle policies without changing agent implementations. Add a bounded hook layer that enforces decisions outside the model and records their outcomes.

## What Changes

- Introduce declarative pre-tool, post-tool and tool-error hooks with tool/agent matchers.
- Support deny-before-execution, suppress-result-after-execution and audit-only rules; policies cannot grant extra permissions or execute arbitrary code.
- Validate operator configuration at startup and apply it consistently to planner, normal/escalated executors and judge, including factory leases.
- Correlate sanitized audit records with workflow ownership; fail closed on hook/audit failure.
- Enforce tool-call limits locally even when a provider emits oversized batches or ignores the final-answer request.

## Capabilities

### New Capabilities

- `tool-lifecycle-hooks`: Configurable lifecycle policies, bounded tool execution and safe audit integration.

### Modified Capabilities

None.

## Impact

ToolLoop, agent dependency injection, settings, container, workflow invocation, audit integration, tests and operator documentation. No new dependency, external API call, public deployment or credential change.

## Non-goals

MCP, skill loading, shell/plugin hooks, session hooks, context compaction, policy editing over HTTP, and hooks around artifact application/build/delivery are separate changes. This layer governs agent exploration tools, not every operation in the factory.
