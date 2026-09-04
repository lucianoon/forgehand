## 1. Policy and dispatch

- [x] 1.1 Add immutable validated operator rules and a fail-closed metadata-only audit dispatcher.
- [x] 1.2 Integrate lifecycle decisions into ToolLoop and enforce call/forced-final limits locally.

## 2. Runtime integration

- [x] 2.1 Wire settings and dispatcher into every agent, including escalated and lease-bound agents.
- [x] 2.2 Bind trusted workflow audit scope during execution and preserve concurrent isolation.

## 3. Verification and handoff

- [x] 3.1 Add regression tests for policy, audit failure/timeout, integration, concurrency and bounded execution.
- [x] 3.2 Document configuration and limitations, run tests/lint/types and validate OpenSpec artifacts.
