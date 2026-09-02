# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/);
versionamento segue [SemVer](https://semver.org/lang/pt-BR/).

## [Não publicado]

### Adicionado

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

[Não publicado]: https://github.com/lucianoon/forgehand/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/lucianoon/forgehand/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/lucianoon/forgehand/releases/tag/v0.1.0
