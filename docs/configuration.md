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

## Arquivo .env lido pelas Settings

Por padrão `Settings()` lê o `.env` do diretório atual. `FORGEHAND_ENV_FILE`
aponta para outro arquivo; vazio desliga a leitura (é o que `tests/conftest.py`
faz, para que a suíte não herde backends, caminhos e chaves do operador).

```bash
FORGEHAND_ENV_FILE=/etc/forgehand/.env.prod uv run uvicorn app.main:app
```

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

Para usar OpenAI diretamente com a chave salva em `.env.local`:

```bash
LLM_PROVIDER_BACKEND=openai uv run uvicorn app.main:app --env-file .env.local --host 127.0.0.1
```

O piloto usa `gpt-4.1-mini-2025-04-14`, com preços de entrada, cache e saída
separados. Veja [OpenAI direto](openai.md) para workers, custos e limitações.
Não há fallback de credenciais entre OpenAI e outros provedores.

OpenRouter continua disponível:

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

## Judge independente do executor

Um modelo tende a aprovar o próprio trabalho. O judge, por isso, é um papel
com bindings próprios no router e registra na avaliação quais modelos
julgaram e se algum deles foi o que executou a tarefa
(`EvaluationResult.judge_models`, `independent_judge`).

```bash
# outro modelo (ou fornecedor) só para o papel "judge", por tier
JUDGE_TIER_BINDINGS_JSON='{"2": {"provider_name": "anthropic", "model": "claude-opus-5"}}'

# bindings (default): usa os bindings acima e apenas registra a coincidência
# escalate: pede ao router outro modelo (tier acima, depois abaixo) quando o
#           do judge coincide com o do executor — garante independência, custa mais
# off: não registra
JUDGE_INDEPENDENCE=bindings

# tarefas com is_critical=true recebem N vereditos; todos precisam aprovar.
# Em `escalate`, o segundo juiz também evita o modelo do primeiro. 1 desliga.
JUDGE_CRITICAL_QUORUM=2
```

No quórum, cada critério recebe a menor nota entre os juízes; as falhas de
quem reprovou entram prefixadas com o modelo e uma linha `[quorum]` registra
a divergência. Com os defaults (sem bindings de judge), o comportamento é o
anterior — mesmo modelo — mas `independent_judge=false` fica visível na
avaliação para quem quiser cobrar a configuração.

## Critérios de aceitação tipados

Cada tarefa do plano carrega `acceptance_criteria` como objetos
`{text, kind, ...}`. O `kind` decide quem verifica:

| kind | parâmetros | como é decidido |
|---|---|---|
| `subjective` | — | judge LLM (score 0–1, aprova com ≥ 0.7) |
| `tests_pass` / `lint_pass` / `types_pass` | — | sinal `pytest` / `ruff` / `mypy` do workspace |
| `file_created` | `path` | diff da tarefa tem `path` com `change_type=created` |
| `file_modified` | `path` | diff da tarefa tem `path` com `change_type=modified` |
| `file_unchanged` | `path` | `path` não aparece entre os arquivos alterados pela tarefa |
| `no_existing_file_modified` | — | todas as mudanças são criações (`op=create`); só para tarefas que criam arquivos novos |
| `changes_limited_to` | `paths` (globs) | todos os arquivos alterados casam com algum glob |
| `content_contains` | `path`, `pattern` (regex) | conteúdo final publicado de `path` casa com `pattern` |
| `citations_valid` | — | `citations` existem no grounding e estão no escopo da tarefa |

Os objetivos entram em `criteria_scores` como 1.0 ou 0.0 sem passar pelo
LLM, e uma falha vira `required_changes` acionável (ex.: "Crie o arquivo X
(op=create)"). Quando não há dado para decidir — sem workspace runtime, sem o
validador configurado, sem grounding — o critério é entregue ao LLM marcado
como "não verificável automaticamente". Se todos os critérios forem
objetivos e verificáveis, o judge não chama o LLM.

O planner recebe essa tabela no prompt e é orientado a preferir kinds
objetivos. Critérios em string (planos ou checkpoints antigos) continuam
aceitos: viram `subjective`, exceto as formulações "alteração mínima /
restrita ao arquivo novo" e "citações válidas", inferidas como
`no_existing_file_modified` e `citations_valid`.

## Referências web na solicitação

Opt-in. Com `WEB_REFERENCES_ENABLED=true`, as URLs http(s) presentes no texto
do pedido são buscadas **uma vez pelo controlador** ao carregar o contexto e
viram evidências `[W1]`, `[W2]`... no mesmo circuito de citações do grounding
do repositório: planner, executor e judge as recebem no prefixo cacheável, e
`citations_valid` aceita esses ids. O sandbox continua sem rede.

```bash
export WEB_REFERENCES_ENABLED=true
# opcional: sufixos de host permitidos (vazio = qualquer host público em 80/443)
export WEB_REFERENCES_ALLOWED_HOSTS=docs.python.org,fastapi.tiangolo.com
export WEB_REFERENCES_MAX_URLS=5            # URLs além disso são listadas, não buscadas
export WEB_REFERENCES_MAX_BYTES=512000      # bytes lidos por página
export WEB_REFERENCES_MAX_CHARS=12000       # caracteres por página no prompt
export WEB_REFERENCES_TIMEOUT_SECONDS=10
# atrás de proxy com CA corporativo: PEM somado ao bundle do certifi
export WEB_REFERENCES_CA_BUNDLE=/etc/ssl/certs/empresa-root.pem
```

Sem o bundle, uma busca atrás de interceptação TLS falha com
`CERTIFICATE_VERIFY_FAILED` e a evidência fica com `status: error` apontando
para esta variável. No Windows, o PEM pode ser exportado do repositório de
certificados com PowerShell (`Get-ChildItem Cert:\LocalMachine\Root`).

Guardas, sempre ativas: só `http`/`https`; o host é resolvido antes da conexão
e endereços privados, loopback, link-local, reservados ou multicast são
recusados (inclusive a cada salto de redirecionamento, máximo três); porta fora
de 80/443 só para host da allowlist; só content-type textual; HTML vira texto
sem `script`/`style`. Uma URL recusada ou inacessível entra como evidência com
`status: error`, para o agente saber que não pôde lê-la. O conteúdo baixado é
apresentado aos agentes como externo e não confiável, nunca como instrução.
DNS rebinding entre a resolução e a conexão não é coberto; use a allowlist em
ambientes sensíveis.

## Tool-use dos agentes

Planner, executor e judge podem explorar o workspace antes de responder, em
vez de dependerem só do recorte do grounding. As ferramentas são poucas e
auditáveis:

| ferramenta | quem tem | o que faz |
|---|---|---|
| `read_file` | todos | lê um arquivo com numeração de linhas (intervalo opcional) |
| `list_directory` | todos | lista um diretório |
| `search_repository` | todos | regex sobre arquivos de texto, devolve `path:linha: texto` |
| `run_check` | executor | roda uma verificação já configurada (`pytest`, `ruff`, `mypy`) pelo nome |
| `fetch_url` | planner e executor (opt-in) | busca uma página web e devolve o texto legível, com as guardas de [referências web](#referências-web-na-solicitação) |

Toda leitura fica dentro do root (executor e judge usam
`EXECUTOR_WORKSPACE_ROOT`; o planner usa `REPOSITORY_ROOT`), diretórios
ignorados (`.git`, `.venv`, `node_modules`...) e arquivos sensíveis (`.env`,
chaves, credenciais) são bloqueados, e `run_check` só executa comandos que já
passaram pelo allowlist do `CommandPolicy` — o modelo escolhe o nome, nunca o
comando.

O loop tem dois tetos: número de chamadas por papel e o budget de tokens
restante da tarefa. Ao atingir qualquer um, a rodada seguinte força a resposta
final estruturada. O que foi explorado fica em `result.exploration` da tarefa
(nome, argumentos, sucesso e um preview de cada chamada), visível ao judge, ao
advisor e no checkpoint.

```bash
AGENT_TOOLS_ENABLED=true                # false desliga para todos os papéis
AGENT_TOOLS_MAX_CALLS_EXECUTOR=8        # 0 desliga só para este papel
AGENT_TOOLS_MAX_CALLS_PLANNER=4
AGENT_TOOLS_MAX_CALLS_JUDGE=4
AGENT_TOOLS_MAX_OUTPUT_CHARS=12000      # corte por resultado de ferramenta
AGENT_TOOLS_ALLOW_CHECKS=true           # oferece run_check ao executor
AGENT_WEB_FETCH_ENABLED=false           # oferece fetch_url (usa WEB_REFERENCES_* como guarda)
AGENT_WEB_FETCH_ROLES=planner,executor  # papéis que recebem a ferramenta
```

`fetch_url` compartilha o coletor das referências web: mesma allowlist, mesma
resolução prévia contra SSRF, mesmos limites de bytes/caracteres e o mesmo
`WEB_REFERENCES_CA_BUNDLE`. Uma URL recusada volta ao modelo como erro de
ferramenta explicando o motivo; o texto de uma página lida chega marcado como
externo e não confiável. Cada chamada conta no teto do papel e passa pelos
[hooks](tool-hooks.md), então `{"event":"pre_tool","tool":"fetch_url","action":"deny"}`
bloqueia a ferramenta sem desligar a configuração.

Nos providers, a resposta final continua sendo a ferramenta de saída
estruturada (`emit_structured_output`, sempre a primeira da lista). Na
Anthropic o `tool_choice` é `any` enquanto o modelo pode explorar e aponta
para a saída na rodada final; no OpenAI-compatible as ferramentas viram
functions com `tool_choice=required` e a saída estruturada deixa de usar
`response_format` nessas chamadas.

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

### Operações de arquivo

O executor devolve `operations`, aplicadas em ordem pelo workspace runtime:

| op | campos | uso |
|---|---|---|
| `create` | `path`, `content` | arquivo novo (conteúdo completo) |
| `replace` | `path`, `search`, `replace`, `occurrence?` | trecho de arquivo existente |
| `delete` | `path` | remover arquivo |

`search` precisa ser um trecho literal e único do arquivo atual; se aparecer
mais de uma vez o executor deve ampliar o trecho ou informar `occurrence`
(1 = primeira). O casamento é exato e, para trechos multilinha, tolera CRLF e
espaços à direita. Uma operação que não pode ser aplicada (trecho ausente,
ambíguo, arquivo inexistente) não derruba a tarefa: entra em
`workspace.apply_errors`, vira o sinal `apply: failed` no feedback do
autocorrect e veta a aprovação do judge até ser corrigida.

O runtime também grava `workspace.published_files` (conteúdo final de cada
arquivo tocado) e `workspace.deleted_paths`; é isso que
`POST /workflows/{id}/pull-request` publica. Payloads antigos com `files`
(arquivo inteiro) continuam aceitos e são tratados como `create`.

Para que o executor consiga editar qualquer ponto de um arquivo, ele precisa
ter visto o texto. `REPOSITORY_GROUNDING_FULL_FILE_MAX_BYTES` (default `0`,
desligado) faz arquivos até esse tamanho entrarem inteiros na evidência, em
vez do recorte de `REPOSITORY_GROUNDING_MAX_LINES_PER_FILE` linhas. Um valor
como `12000` cobre arquivos de até ~300 linhas; avalie o impacto no tamanho do
prompt (o grounding é cacheado, então o custo recorrente é baixo).

Executor operacional (opt-in):

```bash
# checkout do projeto-alvo; o default ./data/executor-workspace é um diretório
# dedicado e vazio — nunca aponte para o diretório do servidor Forgehand
export EXECUTOR_WORKSPACE_ROOT=/srv/projetos/alvo
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
