## ADDED Requirements

### Requirement: Validated operator policy
The system SHALL load bounded, immutable rules from operator configuration, reject invalid events/actions, duplicate IDs and unknown fields, and treat an empty list as disabled.

#### Scenario: Invalid configuration
- **WHEN** a rule attempts a shell command or deny action on post_tool
- **THEN** startup configuration validation fails before any tool executes

### Requirement: Enforced tool lifecycle
The system SHALL run pre_tool before invocation, post_tool after successful invocation and tool_error after failure. Deny SHALL prevent invocation and suppress SHALL prevent output delivery. Restrictive decisions SHALL win over audit-only rules without bypassing built-in policies.

#### Scenario: Denied call
- **WHEN** an executor calls a tool matching a deny rule
- **THEN** the tool is not invoked and the model receives a safe error result

#### Scenario: Suppressed result
- **WHEN** a post rule suppresses a successful tool result
- **THEN** the original result is absent from subsequent model messages and trace previews, and no rollback is claimed

#### Scenario: Tool error
- **WHEN** a tool raises an exception
- **THEN** tool_error is recorded without post_tool and cancellation is not converted to success

### Requirement: Correlated fail-closed audit
When hooks are enabled the system SHALL record metadata-only lifecycle events with trusted ownership, agent/task identity and generated call correlation. Audit failure or timeout SHALL abort without leaking exception bodies. Concurrent workflows SHALL retain separate ownership.

#### Scenario: Pre audit unavailable
- **WHEN** audit fails or times out before tool execution
- **THEN** the tool does not run and a safe hook failure propagates

#### Scenario: Post audit unavailable
- **WHEN** audit fails after tool execution
- **THEN** the output is withheld and the loop fails without claiming rollback

### Requirement: Complete agent integration
The system SHALL apply configured hooks to planner, judge, normal and escalated executors, including lease-bound factory agents.

#### Scenario: Lease isolation
- **WHEN** a factory creates agents for a lease
- **THEN** those agents use the configured dispatcher while retaining their original workspace restrictions

### Requirement: Locally enforced execution bounds
The system SHALL enforce call limits within provider batches, count denied and unknown calls, and stop without executing additional tools if a forced-final response still requests tools.

#### Scenario: Oversized batch
- **WHEN** a provider returns more tool calls than the remaining limit
- **THEN** only remaining permitted slots are attempted and excess calls receive limit errors

#### Scenario: Provider ignores final request
- **WHEN** a forced-final response requests more tools
- **THEN** the loop terminates with a safe error and does not invoke them
