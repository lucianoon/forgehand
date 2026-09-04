## ADDED Requirements

### Requirement: Operator-owned acceptance contract

Acceptance-enabled profiles SHALL define bounded CLI cases outside the candidate repository and map each case to an exact approved criterion. Selection MUST reject uncovered work-order criteria and pin suite/case identities before planning.

#### Scenario: Uncovered requirement
- **WHEN** an order contains a criterion absent from its configured acceptance suite
- **THEN** strategy selection is unsupported rather than claiming full acceptance

#### Scenario: Configuration drift
- **WHEN** expected output, command or case identity changes after selection
- **THEN** registry reconstruction rejects that selection

### Requirement: Independent isolated comparison

The runner SHALL execute acceptance cases after regular build phases in network-disabled disposable containers with a read-only workspace. The host SHALL compare exact captured stdout against the operator expectation before diagnostic sanitization. Only complete exit-zero executions with successful cleanup and matching output SHALL pass.

#### Scenario: Self-authored green tests with incorrect behavior
- **WHEN** ordinary tests report success but the program produces the wrong acceptance output
- **THEN** independent acceptance fails and the build cannot be published

#### Scenario: Output manipulation or incomplete execution
- **WHEN** output only matches after stripping control characters, capture is truncated, execution times out or cleanup fails
- **THEN** the case does not pass

### Requirement: Persisted evidence veto

The graph and publication gate MUST reject required acceptance evidence that is missing, incomplete, failed or does not match pinned suite/case identities and criteria. Review summaries SHALL distinguish independently checked criteria from profiles with no suite.

#### Scenario: Model or aggregate claims success
- **WHEN** the judge approves or a report claims aggregate success without matching acceptance evidence
- **THEN** the workflow cannot publish a verified delivery

#### Scenario: Verified bounded behavior
- **WHEN** every pinned case passes and covers every required criterion
- **THEN** the report records scoped acceptance success without implying universal correctness or automatic merge

### Requirement: Compatibility and bounded operational scope

Legacy profile fingerprints SHALL remain stable, and documentation MUST state opt-in activation, exact-output/CLI limitations and mandatory operator review.

#### Scenario: Existing profile without acceptance
- **WHEN** an existing profile is loaded without a suite
- **THEN** its fingerprint and behavior remain compatible and its review summary does not claim independent acceptance
