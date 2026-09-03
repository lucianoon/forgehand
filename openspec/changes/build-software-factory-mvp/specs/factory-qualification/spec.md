## ADDED Requirements

### Requirement: Real programming benchmark
The project SHALL provide an opt-in benchmark that runs real code-changing work orders against versioned fixture repositories and exercises the same API, queue, worker, graph, sandbox, publication, and CI-result paths used in factory mode.

#### Scenario: Qualification benchmark is started
- **WHEN** an operator explicitly starts the factory qualification benchmark with an LLM provider and GitHub test destination configured
- **THEN** each case begins from a clean pinned fixture and attempts a code change with executable acceptance tests

### Requirement: Representative cases
The benchmark SHALL contain at least five independently repeatable cases spanning defect repair, feature addition, test addition, refactoring with preserved behavior, and documentation or configuration coupled to executable validation.

#### Scenario: Benchmark case definition is validated
- **WHEN** benchmark cases are loaded
- **THEN** every case declares its base SHA, request, acceptance criteria, hidden or independent validation, maximum cost, timeout, and expected changed-file scope

### Requirement: Qualification metrics
The benchmark SHALL report completion, green-PR rate, first-pass rate, human-intervention rate, technical-failure rate, isolation violations, tokens, cost, mean duration, and p95 duration.

#### Scenario: Qualification run completes
- **WHEN** all cases finish or time out
- **THEN** the system emits machine-readable and human-readable reports with per-case evidence and aggregate metrics

### Requirement: MVP release gate
The factory MVP SHALL require at least four of five cases to produce a pull request whose independent checks pass, zero isolation violations, and zero unclassified technical failures.

#### Scenario: Gate is not met
- **WHEN** a qualification run falls below any required threshold
- **THEN** the benchmark exits unsuccessfully and identifies the cases and thresholds responsible

### Requirement: Explicit cost and side-effect controls
The benchmark MUST run only through explicit manual invocation and SHALL enforce per-case and total cost limits while identifying all remote branches and pull requests it creates.

#### Scenario: Total benchmark budget is reached
- **WHEN** accumulated estimated cost reaches the configured total limit
- **THEN** no new case starts, active cases are cancelled safely, and the report marks the run as budget-exhausted
