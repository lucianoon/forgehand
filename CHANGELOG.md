# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/);
versionamento segue [SemVer](https://semver.org/lang/pt-BR/).

## [Não publicado]

### Adicionado

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

### Adicionado

- Política de reporte de vulnerabilidades em `SECURITY.md` e reporte privado
  habilitado no GitHub.
- CodeQL, Dependabot e política de atualizações compatível com os runtimes
  suportados.
- Resultado medido do piloto técnico destacado no README, com metodologia,
  metas e limitações explícitas.
- Captura real do mission control e roteiro para reproduzir o dashboard sem
  consumo de tokens.

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

[Não publicado]: https://github.com/lucianoon/forgehand/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/lucianoon/forgehand/releases/tag/v0.1.0
