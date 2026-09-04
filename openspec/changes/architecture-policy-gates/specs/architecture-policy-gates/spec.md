## ADDED Requirements

### Requirement: Operator-owned architecture policy
The system SHALL accept bounded versioned Python import rules only through approved build profiles and pin policy identity into build selection. Profiles without policy MUST preserve prior fingerprints and behavior.

#### Scenario: Policy altered after selection
- **WHEN** a configured rule changes after selection
- **THEN** the runner rejects profile drift rather than applying or ignoring the changed policy

### Requirement: Deterministic bounded import analysis
The checker SHALL analyze absolute and relative Python imports without executing repository code, report violations with rule/file/line/dependency/remediation, and fail closed on unsupported recognized imports, inaccessible or malformed source and exceeded limits.

#### Scenario: Forbidden relative import
- **WHEN** a governed module imports a forbidden module with relative syntax
- **THEN** the report identifies the resolved dependency and violated rule

#### Scenario: Unsafe or incomplete source tree
- **WHEN** the source contains symlinks, unreadable files, invalid Python, no matching source modules or exceeds scan limits
- **THEN** the report fails without following links or claiming complete validation

### Requirement: Architecture is an objective build and publication gate
The runner SHALL evaluate configured policy before phases and after successful phases. Publication MUST reject missing, failing or wrong-policy evidence even if the aggregate build result claims success.

#### Scenario: Violation before build
- **WHEN** the initial scan violates a rule
- **THEN** no build phase runs and the failure is persisted

#### Scenario: Missing evidence in a green report
- **WHEN** selection requires architecture evidence but the report lacks matching passing evidence
- **THEN** publication is blocked

### Requirement: Correction feedback preserves evidence
Architecture findings SHALL enter the existing objective feedback and review summary with actionable location and remediation, without changing retry budgets or granting permission to edit policies.

#### Scenario: Agent retries after a violation
- **WHEN** a build fails on an import boundary
- **THEN** the next attempt receives the violated rule, source location, target and corrective guidance
