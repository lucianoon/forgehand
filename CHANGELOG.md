# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/);
versionamento segue [SemVer](https://semver.org/lang/pt-BR/).

## [Não publicado]

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

[Não publicado]: https://github.com/lucianoon/forgehand/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/lucianoon/forgehand/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/lucianoon/forgehand/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/lucianoon/forgehand/releases/tag/v0.1.0
