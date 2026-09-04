# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/);
versionamento segue [SemVer](https://semver.org/lang/pt-BR/).

## [Não publicado]

### Corrigido

- Rodadas reais com Claude (03 e 04/09/2026) expuseram e corrigiram:
  - executor e judge exploravam o diretório do próprio servidor: o default de
    `EXECUTOR_WORKSPACE_ROOT` passa a ser `./data/executor-workspace`, criado
    sob demanda, e o `.env.demo` aponta planner e executor para um workspace
    dedicado;
  - a entrega final embutia o conteúdo num repr de dict Python: `synthesize`
    renderiza resumo, notas e arquivos aplicados (`render_task_result`);
  - `GET /workflows/{id}/details` devolvia 500 para workflow cancelado na fila
    ou inexistente; agora 404;
  - no Windows, `python -m pytest` disparado pelo servidor sob `uv run` caía
    no interpretador base sem pytest (CreateProcess procura primeiro no
    diretório do executável pai): o runner local resolve argv[0] pelo PATH;
  - a segunda rodada de autocorreção sem operações apagava
    `applied_files`/`published_files` da rodada anterior — um PR publicaria
    nada. A evidência de workspace é cumulativa entre rodadas;
  - pytest com exit 5 (nenhum teste coletado) contava como reprovação e
    disparava rodada de autocorreção inútil; vira sinal ausente (`None`);
  - a tentativa julgada ficava com `outcome=running` para sempre; recebe o
    veredito e o motivo;
  - `StructuredOutputError` passa a informar `stop_reason` (em especial
    truncamento por `max_tokens`) e um trecho do texto emitido.
- O aplicativo volta a importar e servir o mission control no Windows: `fcntl`,
  `os.killpg`, `os.getuid` e as flags `O_NOFOLLOW`/`O_DIRECTORY` ficaram
  confinados em `app/infrastructure/posix.py`. O factory mode continua
  exigindo POSIX e falha fechado com `PosixRequired`; a lógica de matar o
  grupo de processos deixou de estar duplicada entre workspace e runtime.
- A suíte de testes não lê mais o `.env` do operador: `FORGEHAND_ENV_FILE`
  escolhe o arquivo lido pelas `Settings` e vazio desliga a leitura, o que o
  `conftest` faz. Um `AUDIT_LOG_PATH` real fora da CI derrubava dez testes.
- Testes de API que constroem o factory mode passam a ser pulados quando o
  binário `docker` não está no PATH, em vez de falhar na inicialização.

### Alterado

- `app/graph/nodes.py` (uma função de ~1.200 linhas) foi dividido por fase do
  grafo: `contracts` (protocolos e `NodeDependencies`), `build_evidence`
  (veto e relatórios), `phase_setup`, `phase_execution`, `phase_review` e
  `phase_delivery`. `build_nodes(...)` e os tipos re-exportados mantêm a API;
  o allowlist do checkpoint aceita `ExecutionPayload` nos dois caminhos.

## [0.4.1] — 2026-09-02

Primeira rodada com LLM real (Claude Sonnet 5) contra um projeto-alvo com CI:
o ciclo correção → PR → CI verde fechou de primeira, e os cinco achados da
rodada entram aqui.

### Adicionado

- Benchmark aceita `delivery` por caso (mesmo shape de `POST /workflows`) e
  devolve o `delivery` do status no resultado — permite medir o ciclo
  completo até o PR verde em um repositório de teste.

- Critério `file_unchanged` (path): o arquivo não pode ser alterado nem
  removido pela tarefa. Cobre "não altere os testes", que antes acabava
  mapeado para `no_existing_file_modified` (só criações) e reprovava qualquer
  edição legítima. Prompt do planner esclarece a diferença.

### Corrigido

- Publicação no GitHub cria a branch da entrega só depois do commit, já
  apontando para ele: antes a ref nascia na base e era movida em seguida, o
  que disparava uma execução de CI vermelha inútil no commit antigo.
- Planner passa a saber quais capabilities não gravam arquivos (execution
  strategies com `apply_files=false`, ou aplicação desligada) e deixa de
  exigir `file_created`/`content_contains` nelas — antes, uma análise em
  `research` planejava "criar documento", o executor não gravava e o judge
  reprovava.
- Saída estruturada embrulhada em uma chave única (`{"parameters": {...}}`,
  visto com Claude Sonnet 5 no judge) passa a ser desembrulhada antes de
  falhar a validação, em vez de escalar a tarefa por erro do judge.
- `temperature` deixou de ser enviado por padrão aos fornecedores: os modelos
  Claude 5 rejeitam o parâmetro (400 "deprecated for this model"), o que
  derrubava toda chamada com a configuração padrão. `CompletionRequest.
  temperature` passa a ser opcional (`None` = não enviar) nos dois providers.

## [0.4.0] — 2026-09-02

O judge deixa de julgar por texto e de julgar o próprio trabalho: critérios
tipados decididos por código e um papel de judge com modelo próprio e quórum
para tarefas críticas.

### Adicionado

- Judge independente do executor: bindings por papel no `ProviderRouter`
  (`JUDGE_TIER_BINDINGS_JSON`), `CompletionRequest.role` / `avoid_models`,
  modo `JUDGE_INDEPENDENCE` (`bindings` | `escalate` | `off`) e quórum para
  tarefas críticas (`JUDGE_CRITICAL_QUORUM`, unanimidade, menor nota por
  critério). `EvaluationResult` registra `judge_models` e
  `independent_judge`.

- Critérios de aceitação tipados: `AcceptanceCriterion` com `kind`
  (`subjective`, `tests_pass`, `lint_pass`, `types_pass`, `file_created`,
  `file_modified`, `no_existing_file_modified`, `changes_limited_to`,
  `content_contains`, `citations_valid`). Os objetivos são decididos por
  código a partir do workspace, dos validadores e do grounding
  (`app/agents/criteria.py`); o judge LLM recebe só os subjetivos, numerados,
  e é pulado quando não há nenhum. Objetivo sem dado para decidir cai para o
  LLM com a nota "não verificável". Planner emite critérios tipados; strings
  continuam aceitas (planos e checkpoints antigos).

### Removido

- Heurísticas de texto do judge para "alteração mínima" e "citações válidas":
  viraram os kinds `no_existing_file_modified` e `citations_valid`
  (inferidos automaticamente para critérios legados em string).

## [0.3.0] — 2026-09-02

Agentes exploram o workspace com ferramentas antes de responder, e a entrega
vira etapa do workflow: um commit atômico, um PR e o CI como veto.

### Adicionado

- Entrega até o PR verde: `delivery` na criação do workflow faz o grafo
  publicar um único commit (Git Data API) na branch da entrega, abrir ou
  reutilizar o PR e esperar os checks do CI. CI vermelho reabre as tarefas
  que publicaram arquivos com as falhas (resumo e anotações) como
  `required_changes` e volta ao replan, limitado por `max_iterations`; depois
  cai no gate humano. Status expõe `delivery`; `final_output` ganha seção
  "Entrega". Rota manual de PR passa a aceitar `wait_for_checks`.
- Autenticação GitHub por App (`GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`,
  `GITHUB_APP_PRIVATE_KEY[_PATH]`, extra `github-app`) além de `GITHUB_TOKEN`.

- Tool-use nos agentes: planner, executor e judge exploram o workspace com
  `read_file`, `list_directory` e `search_repository` (executor também com
  `run_check`, restrito às verificações configuradas) antes de emitir a
  resposta estruturada. Loop com teto de chamadas por papel e pelo budget de
  tokens da tarefa; leituras confinadas ao root, sem diretórios ignorados nem
  arquivos sensíveis. Rastro da exploração em `result.exploration`.
  Configuração via `AGENT_TOOLS_*`.
- Providers Anthropic e OpenAI-compatible falam tool-use: `CompletionRequest`
  ganha `tools` e `force_final`; `CompletionResult` ganha `tool_calls`;
  `Message` carrega `tool_calls`/`tool_results`.

### Alterado

- Publicação no GitHub deixa a Contents API (um commit por arquivo) pela Git
  Data API (árvore + commit atômico); remoções entram no mesmo commit e
  retry com conteúdo idêntico não cria commit.
- `AgentTask.reopen_reason`: uma tarefa aprovada só é rebaixada pelo reducer
  quando reaberta explicitamente por sinal externo com motivo novo.

## [0.2.0] — 2026-09-02

Executor edita por operações em vez de reescrever arquivos, e o grounding do
repositório passa a ser cacheado no fornecedor de LLM.

### Adicionado

- Executor descreve mudanças como operações (`create`, `replace`, `delete`)
  em vez de arquivo inteiro. `replace` usa trecho literal (`search`) com
  casamento tolerante a CRLF/espaços à direita e exige unicidade ou
  `occurrence`; falha de aplicação vira feedback para o autocorrect e veto
  objetivo no judge. O payload legado `files` continua aceito.
- Workspace expõe `published_files` (conteúdo final) e `deleted_paths`; a
  publicação de PR usa esses dados e passa a remover arquivos na branch.
- `REPOSITORY_GROUNDING_FULL_FILE_MAX_BYTES`: arquivos até esse tamanho entram
  inteiros na evidência, para o executor poder editar qualquer trecho deles.
- Prompt caching na porta única de LLM: `CompletionRequest.cache_prefix`
  leva o grounding do repositório como bloco estável marcado com
  `cache_control` (Anthropic e OpenRouter); tokens de cache são lidos,
  precificados (`cache_read_per_mtok` / `cache_write_per_mtok`) e exportados
  nos spans OTel. `OPENROUTER_PROMPT_CACHING` controla o envio dos blocos.

### Alterado

- Planner, executor e judge recebem o grounding completo do workflow no
  prefixo cacheável e apenas a lista de `evidence_ids` da tarefa no user
  content, em vez de um recorte de até 8 evidências por chamada.
- `Usage.total_tokens` passa a incluir tokens lidos/escritos em cache, que
  ocupam contexto e contam para o budget da tarefa.

- Parágrafo de abertura do README alinhado à descrição pública da plataforma
  (LangGraph, veto objetivo, gate humano, circuit breakers e OTel).
- Resultado medido do piloto técnico destacado no README, com metodologia,
  metas e limitações explícitas.
- Captura real do mission control e roteiro para reproduzir o dashboard sem
  consumo de tokens.

### Segurança

- Política de reporte de vulnerabilidades em `SECURITY.md` e reporte privado
  habilitado no GitHub.
- CodeQL, Dependabot e política de atualizações compatível com os runtimes
  suportados.
- `langgraph-checkpoint-postgres` 3.1.0 → 3.1.2 (CVE-2026-71433).

## [0.1.0] — 2026-07-26

Primeira versão pública, com as sete fases do roadmap implementadas.

### Adicionado

- Orquestração LangGraph com fan-out paralelo, dependências e merge
  determinístico.
- Judge incremental com veto objetivo de `pytest`, `ruff` e `mypy`.
- Checkpoints, fila durável, lease e heartbeat de workers em PostgreSQL.
- Gate humano com aceite parcial, nova tentativa e cancelamento.
- Circuit breakers de tokens, custo, tempo e tentativas.
- Memória de projeto opcional em Neo4j.
- Observabilidade com OpenTelemetry/Langfuse, métricas e auditoria.
- Mission control web, integração com GitHub e publicação opcional de pull
  requests.
- Suíte com 95 testes unitários e de integração.

[Não publicado]: https://github.com/lucianoon/forgehand/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/lucianoon/forgehand/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/lucianoon/forgehand/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/lucianoon/forgehand/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/lucianoon/forgehand/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/lucianoon/forgehand/releases/tag/v0.1.0
