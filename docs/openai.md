# Direct OpenAI backend

Set `LLM_PROVIDER_BACKEND=openai` to use the official API directly. The existing
Anthropic/OpenRouter backends remain available and the global default is unchanged.
The OpenAI branch reads only `OPENAI_API_KEY`, connects to `https://api.openai.com`,
and sends no OpenRouter routing, healing, or Anthropic cache-control extensions.
`OPENAI_BASE_URL` and `OPENROUTER_BASE_URL` do not redirect this backend.

## Local startup

Create/save the key using the secure setup flow, never in chat or tracked files.
For a key saved in the ignored `.env.local`, start from the repository root:

```sh
LLM_PROVIDER_BACKEND=openai uv run uvicorn app.main:app --env-file .env.local \
  --host 127.0.0.1 --port 8000
```

For a separate worker, load the same file with the launcher:

```sh
LLM_PROVIDER_BACKEND=openai uv run --env-file .env.local python -m app.worker
```

`Settings` deliberately does not store API secrets. Merely putting the key in
`.env.local` does not export it: use the loader above or your deployment's secret
injection. Missing credentials fail before a workflow can make model calls.
Do not put the key in build profiles, work orders, or fixture repositories.

## Model and accounting

The bounded pilot uses `gpt-4.1-mini-2025-04-14` for all three tiers. This is a
compatibility baseline, not a claim that it is the newest or best coding model.
There is no automatic expensive upgrade or model-independent judge by default.
Override `TIER_BINDINGS_JSON`, `JUDGE_TIER_BINDINGS_JSON`, and `PRICING_JSON`
together when changing the experiment; include prices for every chosen model.

The official [model page](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
lists Chat Completions, function calling, structured output and this snapshot.
Standard text prices checked on 2026-09-03 are USD 0.40 input, 0.10 cached input,
and 1.60 output per million tokens. Recheck prices before a paid run. Cached input
is subtracted from ordinary input so the budget does not charge it twice.

The adapter uses `max_completion_tokens`, `store=false`, and locally validated
structured output, following the [OpenAI guide](https://developers.openai.com/api/docs/guides/structured-outputs).
Existing retry, timeout, circuit-breaker and budget controls are retained.
`store=false` is not a claim of zero data retention. Request-level cost accounting
is an estimate, not a hard provider-side billing cap.

## Qualification remains opt-in

The manual qualification workflow accepts `llm_provider=openai` and requires
`OPENAI_API_KEY` plus `FACTORY_GITHUB_TOKEN` in its protected GitHub environment.
The local key is **not** uploaded to GitHub by setup. Only the selected provider's
credential is passed to the runtime. Creating repositories, setting GitHub
secrets, and launching paid cases require separate approval.

Local contract tests use fake credentials and `httpx.MockTransport`; they do not
prove that the account has model access, credits, or permission to run the pilot.
Factory mode stays disabled until the separate five-case release gate passes.

Validation on 2026-09-03: 505 Python tests passed with actual Docker tests enabled;
3 external-service tests were skipped. All 4 dashboard tests, Ruff, and Mypy
(58 source files) passed. The authorized local pilot made real OpenAI calls
and created fixture PRs. See [live results](factory-live-results-2026-09-03.md)
for the release gate and limitations; local tests alone do not qualify delivery.

The pilot exposed two strict-schema incompatibilities. The adapter now removes
Pydantic defaults and converts discriminated `oneOf` unions to `anyOf`. Required
discriminator constants remain mutually exclusive, and responses are still
validated against the original Pydantic models. Regression tests use the actual
planner and executor schemas, not only a simplified demonstration schema.
