# Forgehand

*[Versão em português](README.md)*

[![CI](https://github.com/lucianoon/forgehand/actions/workflows/ci.yml/badge.svg)](https://github.com/lucianoon/forgehand/actions/workflows/ci.yml)
[![Benchmark](https://github.com/lucianoon/forgehand/actions/workflows/benchmark.yml/badge.svg)](https://github.com/lucianoon/forgehand/actions/workflows/benchmark.yml)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Multi-agent software delivery platform: LangGraph orchestration, parallel
execution, a judge with objective veto (pytest/ruff/mypy), a human gate on
critical decisions, cost and time circuit breakers, durable execution and
OTel observability.

## Evidence at a glance

| Capability | Verifiable implementation |
|---|---|
| Parallelism | Independent task fan-out and deterministic merge by ID |
| Quality gate | Incremental judge combined with `pytest`, `ruff` and `mypy` |
| Human control | Approval, retry, partial acceptance and abort at critical decisions |
| Operational limits | Token, cost, time and attempt circuit breakers |
| Durable execution | PostgreSQL checkpoints and restart recovery |
| Observability | OTel/Langfuse spans per job and LLM call |

## Measured result

Technical pilot from July 20, 2026: 9 real workflows across 3 scenarios and
3 rounds, executed with an LLM through OpenRouter after regression fixes.

| KPI | Result | Gate |
|---|---:|---:|
| Completion | 88.9% (8/9) | >= 80% |
| First pass | 88.9% | >= 60% |
| Technical failure | 0% | 0% |
| Average cost | US$0.00291 | <= US$0.05 |
| p95 latency | 41.59 s | <= 120 s |

**Final gate: passed.** See the [methodology, failure analysis, and complete
matrix](docs/pilot-report-2026-07-20.md). These figures come from a
reproducible internal pilot, not an independent public benchmark.

## Mission control

![ForgeHand's actual dashboard with runtime health, budget, and workflow stages](docs/assets/forgehand-dashboard.jpg)

The interface above is served by the application itself and queries `/readyz`
and `/metrics` to display actual runtime health. Reproduce this local state
without external databases:

```bash
make demo
# or, without make (e.g. Windows):
uv sync --extra dev --locked
uv run uvicorn app.main:app --env-file .env.demo
```

Mission control and the operational executor run on any platform. **Factory
mode** (isolated checkout, Docker sandbox, per-workflow POSIX lock) requires
Linux or WSL and fails closed with `PosixRequired` elsewhere.

The [`.env.demo`](.env.demo) profile forces every backend to memory and works
even with a production `.env` in place. Open `http://localhost:8000/dashboard`
and use the local `dev-key`. Running a workflow also requires an LLM provider
(commented at the end of `.env.demo`); opening and validating mission control
does not consume tokens.

## What it does

You POST a request in natural language. A planner decomposes it into tasks
with explicit acceptance criteria and dependencies. Ready tasks fan out in
parallel to capability-specific executors. A judge evaluates each task **on
its own branch, as it finishes** — and its approval is structurally vetoed
when objective signals (`pytest`, `ruff`, `mypy`) fail, so an LLM cannot
approve code that does not compile. Results are consolidated, and the
workflow either replans, synthesizes a deliverable, or pauses at a human gate.

Everything is checkpointed: with `CHECKPOINTER_BACKEND=postgres`, workflows
and pending human interrupts survive a process restart.

## Architecture

```
POST /workflows
      │
  enqueue on a shared queue (queued)
      │
dedicated worker → load_context → create_plan → [route_to_execution]
                                    │ Send × N (parallel, ready_tasks only)
                              execute_task (per-task timeout + budget,
                                    │        incremental judge on the branch)
                                    │ join
                            evaluate_results (consolidation + judge_router)
                          ┌─────────┼──────────┐
                       replan   synthesize  human_gate (interrupt)
                          │         │       accept_partial | retry | abort
                          └────►────┴──────────┘
                              persist_memory → END
```

Layers:

- `app/graph/` — state (single source of truth in `plan`, reducers for safe
  fan-out), nodes and graph assembly. Ordinary dependency waves do not consume
  replan iterations;
- `app/agents/` — planner, per-capability executors, judge and advisor. They
  talk only to the `ProviderRouter`;
- `app/providers/` — the single port to LLMs: retry, circuit breaker, cost
  from an injected price table, validated structured output. Anthropic plus
  any OpenAI-compatible endpoint (local ones included);
- `app/api/` — FastAPI on top of the checkpointer. In production the API only
  enqueues jobs; processing runs in `app.worker` or in the Compose `worker`
  service.

## Architectural rules, with enforcement

Each rule is backed by a mechanism, not by convention:

| Rule | Mechanism |
|---|---|
| Agents never call a provider | `ProviderRouter` is the only port; an agent asks for a tier, not a model |
| Structured output | `response_schema` + Pydantic validation inside the provider |
| Acceptance criteria are mandatory | `min_length=1` in the planner schema + `AgentTask` validator |
| Timeouts | `asyncio.wait_for(task.timeout_seconds)` in the worker |
| Bounded parallelism | `AgentProfile.max_parallel_tasks` caps fan-out per agent |
| Idempotency | deterministic `idempotency_key()` per (project, task, attempt) |
| The judge is not just an LLM | the `EvaluationResult` validator rejects approval while any objective signal fails |
| Expensive models only by escalation | tiers in the registry; `escalate()` steps up one level, fallback degrades downward |
| Traceability | one `TaskAttempt` per attempt + checkpoints queryable over SQL |

## Quickstart

```bash
cp .env.example .env
# edit .env and fill in OPENROUTER_API_KEY
# confirm:
#   LLM_PROVIDER_BACKEND=openrouter
#   OPENROUTER_BASE_URL=https://openrouter.ai/api
docker compose up --build
```

If port 8000 is already taken, pick another one without touching the
container:

```bash
APP_PORT=8001 docker compose up -d --build
curl --fail http://localhost:8001/readyz
```

Without Docker (all-in-one mode, the official local flow with OpenRouter):

```bash
uv pip install -e ".[dev]"
set -a; source .env; set +a
uvicorn app.main:app --reload
```

## Usage

```bash
curl -X POST localhost:8000/workflows \
  -H 'X-API-Key: dev-key' \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "forgehand-demo",
    "request": "Review this project and deliver a short summary with the 3 highest-priority technical next steps."
  }'

curl localhost:8000/workflows/{workflow_id} \
  -H 'X-API-Key: dev-key'

# when status = awaiting_decision:
curl -X POST localhost:8000/workflows/{workflow_id}/decision \
  -H 'X-API-Key: dev-key' \
  -H 'Content-Type: application/json' \
  -d '{"decision": "accept_partial"}'   # or "retry" | "abort"

# cancel a queued or running workflow:
curl -X POST localhost:8000/workflows/{workflow_id}/cancel \
  -H 'X-API-Key: dev-key'
```

Operational endpoints:

```bash
curl localhost:8000/health
curl localhost:8000/readyz
curl localhost:8000/metrics
curl localhost:8000/metrics/prometheus
curl localhost:8000/audit/events -H 'X-API-Key: dev-key'
```

The `POST /workflows` body must be UTF-8. On Windows terminals (Git Bash,
PowerShell) accented text typed inline arrives mangled and the API answers
`There was an error parsing the body`; save the JSON to a file and send it with
`curl --data-binary @req.json -H 'content-type: application/json; charset=utf-8'`.

### Mission control

```text
http://localhost:8000/dashboard
```

The dashboard lets you authenticate with an API key, start workflows, follow
stages, tasks, tokens and cost, answer the human gate and copy the final
deliverable without touching `curl`. Recent history per project lets you
resume an earlier run without keeping IDs by hand.

## LLM providers

OpenRouter is the recommended path:

```bash
export LLM_PROVIDER_BACKEND=openrouter
export OPENROUTER_API_KEY=sk-or-v1-...
export OPENROUTER_BASE_URL=https://openrouter.ai/api
```

With `LLM_PROVIDER_BACKEND=openrouter`, the project uses the
OpenAI-compatible provider with an explicit default binding to
`openai/gpt-4o-mini`, strict JSON Schema, routing that requires compatible
parameters, and response healing. Invalid structured responses are retried by
the provider before the failure escalates to the workflow.

Anthropic remains available as an alternative:

```bash
export LLM_PROVIDER_BACKEND=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
```

## Optional backends

**Persistent project memory (Phase 4)** is opt-in — without it the backend is
in-process memory:

```bash
# in .env: MEMORY_BACKEND=neo4j and NEO4J_PASSWORD=<password>
docker compose --profile neo4j up --build
```

**Dedicated worker with Postgres:**

```bash
export CHECKPOINTER_BACKEND=postgres
export WORKFLOW_QUEUE_BACKEND=postgres
export RUN_EMBEDDED_WORKFLOW_WORKERS=false
export LLM_PROVIDER_BACKEND=openrouter
uvicorn app.main:app --reload

# in another terminal
export CHECKPOINTER_BACKEND=postgres
export WORKFLOW_QUEUE_BACKEND=postgres
python -m app.worker
```

**OTel/Langfuse tracing (Phase 7)** is opt-in too. A single OTLP integration
covers any OTel backend — for Langfuse, just point at the OTLP endpoint:

```bash
export TRACING_BACKEND=otlp
export OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(pk-lf:sk-lf)>"
```

Every LLM call becomes a `gen_ai` span (model, tier, tokens, cost, latency,
error) nested inside the job span; the `trace_id` is recorded on each
`TaskAttempt` for correlation.

## Configuration

Queue and worker tuning:

```bash
export WORKFLOW_QUEUE_POLL_INTERVAL_SECONDS=0.25
export WORKFLOW_QUEUE_LEASE_SECONDS=30
export WORKFLOW_QUEUE_MAX_DELIVERY_ATTEMPTS=3
export AUDIT_LOG_MAX_EVENTS=500
export DEFAULT_TASK_MAX_TOKENS=100000
export DEFAULT_TASK_MAX_COST_USD=3.0
```

### Operational executor (opt-in)

```bash
export EXECUTOR_WORKSPACE_ROOT=.
export EXECUTOR_APPLY_FILES_ENABLED=true
export EXECUTOR_MAX_AUTOCORRECT_ROUNDS=1
export PYTEST_VALIDATION_COMMAND="uv run pytest"
export RUFF_VALIDATION_COMMAND="uv run ruff check ."
export MYPY_VALIDATION_COMMAND="uv run mypy app"
export OBJECTIVE_VALIDATION_PIPELINES_JSON='{"backend":["ruff","mypy","pytest"],"frontend":["pytest"],"documentation":[]}'
export EXECUTOR_STRATEGIES_JSON='{"backend":{"apply_files":true,"run_objective_validation":true,"allow_autocorrect":true},"documentation":{"apply_files":true,"run_objective_validation":false,"allow_autocorrect":false},"review":{"apply_files":false,"run_objective_validation":false,"allow_autocorrect":false}}'
```

When enabled, the executor:

- writes the files returned by the LLM into the local workspace (paths are
  resolved and confined to `EXECUTOR_WORKSPACE_ROOT`), and the judge receives
  objective signals from `pytest`, `ruff` and `mypy` whenever those commands
  are configured;
- runs those same commands right after applying the files and attaches the
  terminal feedback to the task result. On retries the executor receives the
  previous attempt's operational feedback in its own prompt, so correction is
  guided by a real error from the environment;
- records `file_diffs` and `operation_history` in the task result and persists
  an `operational_summary` on every `TaskAttempt` — you can inspect what
  changed in each attempt without relying only on the task's final state;
- preserves the truncated `command`, `exit_code`, `stdout` and `stderr` of
  every objective command executed. When the workspace sits inside a Git
  repository, it also attaches a `git status` and `git diff` snapshot taken
  after the artifacts are applied;
- can run a small internal self-correction loop within the same workflow
  attempt: when `EXECUTOR_MAX_AUTOCORRECT_ROUNDS` is greater than zero,
  objective failures from the post-apply checks feed a new LLM round carrying
  the previous iteration's operational feedback, bounded by the configured
  value.

Configuration is per capability:

- `OBJECTIVE_VALIDATION_PIPELINES_JSON` defines the order and the set of
  checks per capability. Runtime and judge share the same pipeline, which
  avoids running every validator on every task and allows proper handling for
  `backend`, `frontend`, `documentation` and other profiles;
- `EXECUTOR_STRATEGIES_JSON` defines the execution strategy per capability:
  whether the task writes files to the workspace, whether it runs objective
  validation, and whether it may use internal self-correction. This lets
  `review`/`research` behave as purely analytical tasks while
  `backend`/`devops` run the full operational path.

## Tests

```bash
uv run pytest tests/unit tests/integration
```

The PostgreSQL restart tests and the Neo4j memory tests are opt-in so the
default suite stays portable. With local databases available:

```bash
RUN_POSTGRES_TESTS=1 uv run pytest tests/integration/test_postgres_restart.py
RUN_NEO4J_TESTS=1 NEO4J_PASSWORD=<password> uv run pytest tests/integration/test_neo4j_memory.py
```

That module also validates lease renewal and delivery ownership. While a
workflow is running the worker refreshes `locked_at` periodically;
confirmations and failures are only accepted while `locked_by` still belongs
to the worker that picked up the job. External workers also register a
heartbeat in PostgreSQL, and `/readyz` stops returning 200 when no registered
worker is alive. CI brings up PostgreSQL 16 and Neo4j 5 and runs these
scenarios.

Aggregate consumption includes planner, executor and judge calls. The
per-task budget is checked before execution and any overrun becomes an
escalation; a human `retry` decision grants headroom for one additional
attempt and is recorded in the checkpoint.

The integration tests exercise the full graph with real providers over a
mocked HTTP transport — the real request is built and the real response is
parsed.

## Docs

- `docs/integrations.md` — GitHub/PR, sandbox, webhooks, benchmark and RBAC;
- `docs/security-model.md` — boundaries, controls and residual risks;
- `docs/go-to-market.md` — design partner, pilot, demo and ROI metrics;
- `docs/production-runbook.md` — deploy, alerts, incident and rollback.
- [`CHANGELOG.md`](CHANGELOG.md) — version history and upcoming changes.

## Roadmap

- [x] **Phase 1** — vertical functional core (with parallelism and the human gate brought forward)
- [x] **Phase 2** — parallel execution (Send + reducers), timeouts, retries, budgets
- [x] **Phase 3** — advisor: consulted during replan when the objective signals of
  `AdvisorTrigger` fire; injects diagnosis/guidance into the next attempt and is
  the only flow that escalates tier (`tier_escalated`)
- [x] **Phase 4** — persistent project memory in Neo4j
  (`(Project)-[:HAS_WORKFLOW]->(Workflow)-[:EXECUTED]->(Task)`); backend selected by
  `MEMORY_BACKEND=memory|neo4j`, with recent history entering the planner's context
- [x] **Phase 5** — real tools in the judge (objective pipeline with
  `pytest`/`ruff`/`mypy` — see "Operational executor" above)
- [x] **Phase 6** — queues/workers (Postgres with lease/heartbeat), auth with RBAC,
  auditing, Prometheus metrics
- [x] **Phase 7** — OTel/Langfuse tracing over OTLP: a `gen_ai` span per call at the
  `ProviderRouter` single port, a root span per job in the worker, and the
  `trace_id` recorded on every `TaskAttempt`

## License

MIT
