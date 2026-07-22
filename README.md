# Forgehand

Sistema multiagente para desenvolvimento de software: orquestrador,
executores especializados por capacidade, judge com veto objetivo,
memória persistente, execução paralela e gate humano em decisões críticas.

## Subir

```bash
cp .env.example .env
# edite .env e preencha OPENROUTER_API_KEY
# confirme:
#   LLM_PROVIDER_BACKEND=openrouter
#   OPENROUTER_BASE_URL=https://openrouter.ai/api
docker compose up --build
```

Memória de projeto persistente (Fase 4) é opt-in — sem ela o backend é em
memória de processo:

```bash
# no .env: MEMORY_BACKEND=neo4j e NEO4J_PASSWORD=<senha>
docker compose --profile neo4j up --build
```

Tracing OTel/Langfuse (Fase 7) também é opt-in. Uma única integração OTLP
cobre qualquer backend OTel — para Langfuse, basta apontar o endpoint OTLP:

```bash
export TRACING_BACKEND=otlp
export OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(pk-lf:sk-lf)>"
```

Cada chamada de LLM vira um span `gen_ai` (modelo, tier, tokens, custo,
latência, erro) aninhado no span do job; o `trace_id` fica gravado em cada
`TaskAttempt` para correlação.

Se a porta 8000 já estiver ocupada, escolha outra porta sem alterar o
container:

```bash
APP_PORT=8001 docker compose up -d --build
curl --fail http://localhost:8001/readyz
```

Sem Docker (modo all-in-one, fluxo oficial local com OpenRouter):

```bash
uv pip install -e ".[dev]"
set -a; source .env; set +a
uvicorn app.main:app --reload
```

Worker dedicado com Postgres:

```bash
export CHECKPOINTER_BACKEND=postgres
export WORKFLOW_QUEUE_BACKEND=postgres
export RUN_EMBEDDED_WORKFLOW_WORKERS=false
export LLM_PROVIDER_BACKEND=openrouter
uvicorn app.main:app --reload

# em outro terminal
export CHECKPOINTER_BACKEND=postgres
export WORKFLOW_QUEUE_BACKEND=postgres
python -m app.worker
```

OpenRouter é o caminho recomendado:

```bash
export LLM_PROVIDER_BACKEND=openrouter
export OPENROUTER_API_KEY=sk-or-v1-...
export OPENROUTER_BASE_URL=https://openrouter.ai/api
```

Quando `LLM_PROVIDER_BACKEND=openrouter`, o projeto usa o provider
OpenAI-compatible com binding explícito default para `openai/gpt-4o-mini`,
JSON Schema estrito, roteamento que exige parâmetros compatíveis e response
healing. Respostas estruturadas inválidas são repetidas pelo provider antes de
escalar a falha ao workflow.

Anthropic continua disponível como alternativa:

```bash
export LLM_PROVIDER_BACKEND=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
```

Tuning da fila/worker:

```bash
export WORKFLOW_QUEUE_POLL_INTERVAL_SECONDS=0.25
export WORKFLOW_QUEUE_LEASE_SECONDS=30
export WORKFLOW_QUEUE_MAX_DELIVERY_ATTEMPTS=3
export AUDIT_LOG_MAX_EVENTS=500
export DEFAULT_TASK_MAX_TOKENS=100000
export DEFAULT_TASK_MAX_COST_USD=3.0
```

Executor operacional (opt-in):

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

Quando habilitado, o executor:

- aplica os arquivos retornados pelo LLM no workspace local, e o judge recebe
  sinais objetivos de `pytest`, `ruff` e `mypy` quando esses comandos forem
  configurados;
- roda esses mesmos comandos logo após a aplicação dos arquivos e anexa o
  feedback de terminal ao resultado da tarefa. Em retries, o executor recebe o
  feedback operacional da tentativa anterior no próprio prompt, permitindo
  correção guiada por erro real do ambiente;
- registra `file_diffs` e `operation_history` no resultado da tarefa e
  persiste um `operational_summary` em cada `TaskAttempt` — dá para
  inspecionar o que mudou em cada tentativa sem depender apenas do estado
  final da tarefa;
- preserva `command`, `exit_code`, `stdout` e `stderr` truncados de cada
  comando objetivo executado. Quando o workspace estiver dentro de um
  repositório Git, anexa ainda um snapshot de `git status` e `git diff` após
  a aplicação dos artefatos;
- pode fazer um pequeno loop interno de autocorreção dentro da mesma tentativa
  do workflow: quando `EXECUTOR_MAX_AUTOCORRECT_ROUNDS` for maior que zero,
  falhas objetivas dos checks pós-aplicação alimentam uma nova rodada do LLM
  com o feedback operacional da iteração anterior, limitada pelo valor
  configurado.

A configuração é por capability:

- `OBJECTIVE_VALIDATION_PIPELINES_JSON` define a ordem e o conjunto de checks
  por capability. Runtime e judge compartilham essa mesma pipeline, evitando
  rodar todos os validadores em toda tarefa e permitindo tratamento adequado
  para `backend`, `frontend`, `documentation` e demais perfis;
- `EXECUTOR_STRATEGIES_JSON` define a estratégia de execução por capability:
  se a tarefa aplica arquivos no workspace, se roda validação objetiva e se
  pode usar autocorreção interna. Isso permite, por exemplo,
  `review`/`research` operarem como tarefas puramente analíticas enquanto
  `backend`/`devops` seguem com execução operacional completa.

Endpoints operacionais:

```bash
curl localhost:8000/health
curl localhost:8000/readyz
curl localhost:8000/metrics
curl localhost:8000/metrics/prometheus
curl localhost:8000/audit/events -H 'X-API-Key: dev-key'
```

Mission control web:

```text
http://localhost:8000/dashboard
```

O dashboard permite autenticar com a API key, iniciar workflows, acompanhar
etapas, tarefas, tokens e custo, responder ao gate humano e copiar a entrega
final sem depender de `curl`. O histórico recente por projeto permite retomar
uma execução anterior sem guardar IDs manualmente.

Integrações e produto:

- `docs/integrations.md` — GitHub/PR, sandbox, webhooks, benchmark e RBAC;
- `docs/security-model.md` — fronteiras, controles e riscos residuais;
- `docs/go-to-market.md` — design partner, piloto, demo e métricas de ROI.
- `docs/production-runbook.md` — deploy, alertas, incidente e rollback.

## Usar

```bash
curl -X POST localhost:8000/workflows \
  -H 'X-API-Key: dev-key' \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "agent-forge-demo",
    "request": "Analise este projeto e entregue um resumo curto com 3 próximos passos técnicos prioritários."
  }'

curl localhost:8000/workflows/{workflow_id} \
  -H 'X-API-Key: dev-key'

# quando status = awaiting_decision:
curl -X POST localhost:8000/workflows/{workflow_id}/decision \
  -H 'X-API-Key: dev-key' \
  -H 'Content-Type: application/json' \
  -d '{"decision": "accept_partial"}'   # ou "retry" | "abort"

# cancela um workflow na fila ou em execução:
curl -X POST localhost:8000/workflows/{workflow_id}/cancel \
  -H 'X-API-Key: dev-key'
```

## Arquitetura

```
POST /workflows
      │
  enqueue em fila compartilhada (queued)
      │
worker dedicado → load_context → create_plan → [route_to_execution]
                                    │ Send × N (paralelo, só ready_tasks)
                              execute_task (timeout + budget por tarefa,
                                    │        judge incremental no branch)
                                    │ join
                            evaluate_results (consolidação + judge_router)
                          ┌─────────┼──────────┐
                       replan   synthesize  human_gate (interrupt)
                          │         │       accept_partial | retry | abort
                          └────►────┴──────────┘
                              persist_memory → END
```

Camadas:

- `app/graph/` — estado (fonte única de verdade em `plan`, reducers para
  fan-out seguro), nós e montagem do grafo. Ondas normais de dependências
  não consomem iterações de replan;
- `app/agents/` — planner, executores por capability, judge e advisor.
  Falam apenas com o `ProviderRouter`;
- `app/providers/` — porta única para LLMs: retry, circuit breaker, custo
  por tabela injetada, saída estruturada validada. Anthropic + qualquer
  endpoint OpenAI-compatible (locais inclusos);
- `app/api/` — FastAPI sobre o checkpointer; workflows e interrupts
  sobrevivem a restart com `CHECKPOINTER_BACKEND=postgres`. Em produção a API
  só enfileira jobs; o processamento roda em `app.worker` ou no serviço
  `worker` do Compose.

## Regras arquiteturais com enforcement

| Regra | Mecanismo |
|---|---|
| Agente não chama fornecedor | `ProviderRouter` é a única porta; agente pede tier, não modelo |
| Saída estruturada | `response_schema` + validação Pydantic no provider |
| Critério de aceitação obrigatório | `min_length=1` no schema do planner + validator do `AgentTask` |
| Timeout | `asyncio.wait_for(task.timeout_seconds)` no worker |
| Paralelismo | `AgentProfile.max_parallel_tasks` limita o fan-out por agente |
| Idempotência | `idempotency_key()` determinística por (projeto, tarefa, tentativa) |
| Judge não é só LLM | validator do `EvaluationResult` rejeita aprovação com sinal objetivo falhando |
| Modelo caro só por escalonamento | tiers no registry; `escalate()` sobe um degrau, fallback degrada para baixo |
| Rastreabilidade | `TaskAttempt` por tentativa + checkpoints consultáveis via SQL |

## Testes

```bash
uv run pytest tests/unit tests/integration
```

Os testes de restart com PostgreSQL e de memória com Neo4j são opt-in para
que a suíte padrão seja portável. Com os bancos locais disponíveis:

```bash
RUN_POSTGRES_TESTS=1 uv run pytest tests/integration/test_postgres_restart.py
RUN_NEO4J_TESTS=1 NEO4J_PASSWORD=<senha> uv run pytest tests/integration/test_neo4j_memory.py
```

Esse módulo também valida renovação de lease e ownership da entrega. Enquanto
um workflow está em execução, o worker atualiza `locked_at` periodicamente;
confirmações e falhas só são aceitas quando `locked_by` ainda pertence ao
worker que recebeu o job. Workers externos também registram heartbeat no
PostgreSQL; `/readyz` deixa de responder 200 quando nenhum worker registrado
está ativo. A CI sobe PostgreSQL 16 e Neo4j 5 e executa esses cenários.

O consumo agregado inclui chamadas de planner, executores e judge. O budget
por tarefa é verificado antes da execução e qualquer ultrapassagem vira
escalation; uma decisão humana `retry` concede headroom para uma tentativa
adicional e fica registrada no checkpoint.

Os testes de integração exercitam o grafo completo com providers reais sobre
transporte HTTP mockado — o request de verdade é montado e o response de
verdade é parseado.

## Roadmap

- [x] **Fase 1** — núcleo funcional vertical (com paralelismo e gate humano antecipados)
- [x] **Fase 2** — execução paralela (Send + reducers), timeouts, retries, budgets
- [x] **Fase 3** — advisor: consultado no replan quando os sinais objetivos do
  `AdvisorTrigger` disparam; injeta diagnóstico/orientação na próxima tentativa
  e é o único fluxo que escala tier (`tier_escalated`)
- [x] **Fase 4** — memória de projeto persistente em Neo4j
  (`(Project)-[:HAS_WORKFLOW]->(Workflow)-[:EXECUTED]->(Task)`); backend via
  `MEMORY_BACKEND=memory|neo4j`, histórico recente entra no contexto do planner
- [x] **Fase 5** — ferramentas reais no judge (pipeline objetiva com
  `pytest`/`ruff`/`mypy` — ver "Executor operacional" acima)
- [x] **Fase 6** — filas/workers (Postgres com lease/heartbeat), auth com RBAC,
  auditoria, métricas Prometheus
- [x] **Fase 7** — tracing OTel/Langfuse via OTLP: span `gen_ai` por chamada na
  porta única do `ProviderRouter`, span raiz por job no worker e `trace_id`
  gravado em cada `TaskAttempt`
