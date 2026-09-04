# Forgehand

*[English version](README.en.md)*

[![CI](https://github.com/lucianoon/forgehand/actions/workflows/ci.yml/badge.svg)](https://github.com/lucianoon/forgehand/actions/workflows/ci.yml)
[![Benchmark](https://github.com/lucianoon/forgehand/actions/workflows/benchmark.yml/badge.svg)](https://github.com/lucianoon/forgehand/actions/workflows/benchmark.yml)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Licença: MIT](https://img.shields.io/badge/licen%C3%A7a-MIT-green.svg)](LICENSE)

Plataforma multiagente de entrega de software: orquestração LangGraph,
execução paralela, judge com veto objetivo (pytest/ruff/mypy), gate humano
em decisões críticas, circuit breakers de custo e tempo, execução durável
e observabilidade OTel.

## Em 30 segundos

- **198 funções de teste** entre cenários unitários e de integração.
- CI com PostgreSQL 16 e Neo4j 5, incluindo restart, lease e heartbeat.
- `pytest`, `ruff` e `mypy` podem vetar uma entrega mesmo quando o judge LLM aprova.
- Tokens, custo, latência, falhas e tentativas são rastreados por workflow.
- O sistema roda localmente com provider simulado; integrações reais são opt-in.

## Evidências rápidas

| Capacidade | Implementação verificável |
|---|---|
| Paralelismo | Fan-out de tarefas independentes e merge determinístico por ID |
| Quality gate | Judge incremental combinado com `pytest`, `ruff` e `mypy` |
| Controle humano | Aprovação, retry, aceite parcial e abort em decisões críticas |
| Limites operacionais | Circuit breakers de tokens, custo, tempo e tentativas |
| Execução durável | Checkpoints em PostgreSQL e retomada após interrupção |
| Observabilidade | Spans OTel/Langfuse por job e chamada de LLM |
| Hooks de ferramentas | [Políticas pre/post/error](docs/tool-hooks.md), bloqueio e auditoria configuráveis |

## Resultado medido

Piloto técnico de 20/07/2026: 9 workflows reais, em 3 cenários e 3 rodadas,
executados com LLM via OpenRouter após as correções de regressão.

| KPI | Resultado | Gate |
|---|---:|---:|
| Conclusão | 88,9% (8/9) | >= 80% |
| First pass | 88,9% | >= 60% |
| Falha técnica | 0% | 0% |
| Custo médio | US$ 0,00291 | <= US$ 0,05 |
| Latência p95 | 41,59 s | <= 120 s |

**Gate final: aprovado.** Consulte a [metodologia, diagnóstico das falhas e
matriz completa](docs/pilot-report-2026-07-20.md). Os números são um piloto
interno reproduzível, não um benchmark público independente.

## Mission control

### Estúdio de produto: ideia → demo

O novo `/studio` transforma uma ideia em escopo editável e backlog. Após aprovação,
gera uma aplicação de cadastros no navegador, com criação, edição, exclusão, busca
e pacote ZIP com código. Histórico persistido em SQLite, acesso por cliente/projeto
e reserva estimada de custo. É uma primeira versão frontend, **não um sistema de
produção com backend ou banco compartilhado**. Desativado por padrão; veja
[como habilitar e usar](docs/product-studio.md).

Também é possível baixar uma [base full-stack independente](docs/fullstack-product-foundation.md)
com login, dados persistentes privados por usuário, PostgreSQL, migração e Docker.
Ela foi verificada em execução local, mas ainda exige regras de negócio e preparação
operacional antes de produção.

![Dashboard real do ForgeHand com runtime, orçamento e etapas do workflow](docs/assets/forgehand-dashboard.jpg)

A interface acima é servida pela própria aplicação e consulta `/readyz` e
`/metrics` para exibir a saúde real do runtime. Para reproduzir o estado local
sem bancos externos:

```bash
make demo
# ou, sem make (ex.: Windows):
uv sync --extra dev --locked
uv run uvicorn app.main:app --env-file .env.demo
```

O mission control e o executor operacional rodam em qualquer plataforma. O
**factory mode** (checkout isolado, sandbox Docker, lock POSIX por workflow)
exige Linux ou WSL e falha fechado com `PosixRequired` em outros hosts.

O perfil [`.env.demo`](.env.demo) força todos os backends para memória e
funciona mesmo com um `.env` de produção presente. Abra
`http://localhost:8000/dashboard` e use a chave local `dev-key`. Executar
um workflow exige também configurar um provider de LLM (comentado no fim do
`.env.demo`); apenas abrir e validar o mission control não consome tokens.

## Início rápido

```bash
cp .env.example .env
# preencha OPENROUTER_API_KEY e confirme LLM_PROVIDER_BACKEND=openrouter
docker compose up --build
```

Mission control em `http://localhost:8000/dashboard`: autentica com a API key,
inicia workflows, acompanha etapas, tarefas, tokens e custo, responde ao gate
humano e copia a entrega final sem depender de `curl`. O histórico recente por
projeto permite retomar uma execução anterior sem guardar IDs manualmente.

```bash
curl localhost:8000/health
curl localhost:8000/readyz
curl localhost:8000/metrics/prometheus
curl localhost:8000/audit/events -H 'X-API-Key: dev-key'
```

Provedor de LLM (OpenAI direto, OpenRouter ou Anthropic), execução sem Docker, worker dedicado
com Postgres, memória persistente em Neo4j, tracing OTel/Langfuse, tuning de
fila e o executor operacional (aplicação de arquivos e validação objetiva por
capability): [docs/configuration.md](docs/configuration.md).

Para usar a chave OpenAI em `.env.local`, consulte [OpenAI direto](docs/openai.md).

Integrações e produto:

- [`docs/integrations.md`](docs/integrations.md) — GitHub/PR, sandbox, webhooks, benchmark e RBAC;
- [`docs/security-model.md`](docs/security-model.md) — fronteiras, controles e riscos residuais;
- [`docs/go-to-market.md`](docs/go-to-market.md) — design partner, piloto, demo e métricas de ROI.
- [`docs/production-runbook.md`](docs/production-runbook.md) — deploy, alertas, incidente e rollback.
- [`CHANGELOG.md`](CHANGELOG.md) — histórico versionado e próximas mudanças.

## Usar

```bash
curl -X POST localhost:8000/workflows \
  -H 'X-API-Key: dev-key' \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "forgehand-demo",
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

Perfis Python da fábrica podem declarar [políticas de arquitetura executáveis](docs/architecture-policies.md):
limites de imports com diagnóstico por arquivo/linha, correção orientada por evidências
e veto de publicação. A política é aprovada pelo operador, não pelo agente gerador.

| Regra | Mecanismo |
|---|---|
| Agente não chama fornecedor | `ProviderRouter` é a única porta; agente pede tier, não modelo |
| Saída estruturada | `response_schema` + validação Pydantic no provider |
| Critério de aceitação obrigatório | `min_length=1` no schema do planner + validator do `AgentTask` |
| Timeout | `asyncio.wait_for(task.timeout_seconds)` no worker |
| Paralelismo | `AgentProfile.max_parallel_tasks` limita o fan-out por agente |
| Idempotência | `idempotency_key()` determinística por (projeto, tarefa, tentativa) |
| Judge não é só LLM | validator do `EvaluationResult` rejeita aprovação com sinal objetivo falhando; critérios tipados (arquivo criado, só criações, conteúdo, testes/lint/tipos, citations) são decididos por código e o LLM só vê os subjetivos |
| Judge não se auto-aprova | papel `judge` com bindings próprios no router; a avaliação registra `judge_models` e `independent_judge`; em `escalate` o router troca de modelo, e tarefas críticas exigem quórum unânime |
| Modelo caro só por escalonamento | tiers no registry; `escalate()` sobe um degrau, fallback degrada para baixo |
| Rastreabilidade | `TaskAttempt` por tentativa + checkpoints consultáveis via SQL |
| Exploração limitada | agentes leem o workspace só por ferramentas confinadas ao root; teto de chamadas e de tokens no `ToolLoop`, `run_check` só por nome do allowlist |
| CI é veto | com `delivery`, o workflow só termina verde: CI vermelho reabre as tarefas que publicaram, com as falhas como `required_changes`, dentro de `max_iterations` |

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

O Studio também oferece [entregas incrementais de produto](docs/incremental-product-delivery.md):
plano persistente ligado a um repositório existente, contexto imutável por tentativa
e avanço condicionado a merge verificado, sempre com execução explicitamente aprovada.

Novas tentativas contam com [admissão atômica e recuperação aprovada](docs/delivery-recovery.md):
o mesmo envio não cria outro job de início; ordens, contexto e limites são preservados.
Recuperação após reinício exige PostgreSQL; não é garantia de efeitos externos exactly-once.

Perfis também podem exigir [aceitação independente de comportamento](docs/independent-acceptance.md):
casos CLI aprovados pelo operador, comparados no host e executados sem escrita no
repositório. Testes verdes ou aprovação do modelo não substituem os casos exigidos.

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
