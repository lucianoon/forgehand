## Context

Factory build profiles are operator-owned, fingerprinted and selected before execution. Build results already persist into attempts, objective feedback and the PR gate. Extend this existing chain instead of adding an independent judge that can disagree with it.

## Goals / Non-Goals

Goals: approved Python import boundaries, evidence-based veto, actionable correction, bounded safe analysis and compatibility when not configured.

Non-goals: JavaScript/TypeScript, whole-program dependency resolution, runtime import/plugin/reflection proofs, security sandbox replacement, autonomous policy changes, CI installation or remote runs.

## Decisions

- Optional architecture policy in a Python build profile: version 1, source roots and rules (id, source module prefix, forbidden target prefixes, remediation). Policy is supplied only through existing operator profile configuration, never loaded from generated repository files.
- Profile fingerprint includes policy only when present, preserving legacy fingerprints. Selection pins the policy digest; publication requires a complete passing report with matching digest whenever a policy was selected.
- Analyze AST static imports, including relative/package imports and aliases; prefix matching respects dot boundaries. Imports inside functions and TYPE_CHECKING count. Wildcard imports and recognized dynamic import calls in governed modules fail with an unsupported-import diagnostic rather than silently passing. Arbitrary alias/data-flow/reflection remains outside static coverage.
- Descriptor-relative no-follow reads; reject symlinks/special files in scanned source trees; bounded files, bytes, directory entries, depth and diagnostics. Do not import source modules, execute code, or print source/error bodies. Empty roots, syntax errors, unmatched rule sources and incomplete scans fail closed.
- Run before phases for early feedback, and after successful phases to catch generated changes. Existing lease serialization remains authoritative. Evidence is a snapshot, not attestation against hostile concurrent changes after validation.
- Persist report in BuildRunResult, attach per-violation objective signals with file/line/remediation, and include a compact architecture section in review output. Preserve budgets and existing retry/human gates.

## Risks / Trade-offs

- Static imports are not all runtime dependencies → explicitly document unsupported reflection and runtime resolution; never market this as a security boundary.
- `from package import name` can reference a module or attribute → conservative candidate dependency matching, documented false positives.
- Whole selected roots include existing debt → strict gate, no automatic baseline/waiver. Operators choose boundaries deliberately.
- Parsing hostile syntax on controller → bound input sizes/depth/work and handle parser errors; no subprocess/import execution. This checker does not replace Docker isolation.
- Profile changes invalidate active selections → same drift behavior as command policy changes; never silently weaken a running policy.

## Migration Plan

Optional fields default absent and legacy profile fingerprints remain unchanged. Configure policies explicitly and restart relevant services to activate; no live policy or service change in this implementation turn. Old workers cannot produce required policy evidence and therefore cannot publish a policy-governed delivery.
