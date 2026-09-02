# Configuração e operação

Referência das opções de configuração do Forgehand. Para subir o projeto em
menos de um minuto, veja o [Início rápido](../README.md#início-rápido).

## Memória persistente (Neo4j)

Memória de projeto persistente (Fase 4) é opt-in — sem ela o backend é em
memória de processo:

```bash
# no .env: MEMORY_BACKEND=neo4j e NEO4J_PASSWORD=<senha>
docker compose --profile neo4j up --build
```

## Tracing OTel / Langfuse

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

## Porta alternativa

Se a porta 8000 já estiver ocupada, escolha outra porta sem alterar o
container:

```bash
APP_PORT=8001 docker compose up -d --build
curl --fail http://localhost:8001/readyz
```

## Execução sem Docker

Sem Docker (modo all-in-one, fluxo oficial local com OpenRouter):

```bash
uv pip install -e ".[dev]"
set -a; source .env; set +a
uvicorn app.main:app --reload
```

## Worker dedicado com Postgres

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

## Provedor de LLM

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

## Prompt caching

O grounding do repositório é o maior bloco repetido entre as chamadas de um
workflow: planner, cada executor e cada judge recebem o mesmo texto. Esse bloco
vai como `cache_prefix` da `CompletionRequest`, antes do system prompt, e os
providers o marcam para cache no fornecedor:

- **Anthropic**: `system` vira dois blocos com `cache_control: ephemeral` —
  o prefixo (compartilhado entre papéis) e o prompt do papel. Blocos abaixo do
  mínimo do modelo (1024 tokens no Sonnet, 2048 no Haiku) são ignorados sem
  custo extra.
- **OpenRouter**: mesmo formato de blocos quando
  `OPENROUTER_PROMPT_CACHING=true` (default). Modelos OpenAI cacheiam o
  prefixo automaticamente e ignoram a marca; endpoints locais recebem o
  prefixo concatenado em texto puro.

Tokens lidos e escritos em cache aparecem em `Usage.cache_read_tokens` /
`Usage.cache_write_tokens`, contam para o budget de tokens da tarefa e são
precificados por `cache_read_per_mtok` / `cache_write_per_mtok` em
`PRICING_JSON`. Os defaults seguem a tabela pública (escrita 1.25x, leitura
0.10x da entrada na Anthropic; leitura 0.5x na OpenAI) — confira antes de
produção. Os spans OTel carregam
`gen_ai.usage.cache_read.input_tokens` e
`gen_ai.usage.cache_creation.input_tokens` para acompanhar a taxa de acerto no
Langfuse.

Para o cache bater, o prefixo precisa ser idêntico entre chamadas: por isso o
grounding completo do workflow vai no prefixo e a seleção por tarefa
(`evidence_ids`) vai no user content como "Evidências atribuídas a esta
tarefa".

## Tuning de fila e worker

Tuning da fila/worker:

```bash
export WORKFLOW_QUEUE_POLL_INTERVAL_SECONDS=0.25
export WORKFLOW_QUEUE_LEASE_SECONDS=30
export WORKFLOW_QUEUE_MAX_DELIVERY_ATTEMPTS=3
export AUDIT_LOG_MAX_EVENTS=500
export DEFAULT_TASK_MAX_TOKENS=100000
export DEFAULT_TASK_MAX_COST_USD=3.0
```

## Executor operacional

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

