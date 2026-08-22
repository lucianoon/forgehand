# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/);
versionamento segue [SemVer](https://semver.org/lang/pt-BR/).

## [Não publicado]

### Adicionado

- Política de reporte de vulnerabilidades em `SECURITY.md` e reporte privado
  habilitado no GitHub.
- CodeQL, Dependabot e política de atualizações compatível com os runtimes
  suportados.
- Resultado medido do piloto técnico destacado no README, com metodologia,
  metas e limitações explícitas.
- Captura real do mission control e roteiro para reproduzir o dashboard sem
  consumo de tokens.
- Fatos verificados com id estável (`app/agents/deterministic_checks.py`), que o
  judge injeta no prompt e reconcilia por id.
- `app/models/contracts.py` com os contratos que atravessam camadas (outcomes
  dos agentes, `UsageReport`, `ExecutionStrategy`).
- `tests/unit/test_layering.py`: as regras de dependência entre camadas passam
  a ser verificadas por AST, em vez de afirmadas em docstring.

### Alterado

- O judge deixou de reconhecer contradições do LLM por substring em português e
  inglês. Cada fato verificado (`citations_valid`, `only_new_files`) tem id
  estável e o schema de saída exige que o LLM marque qual fato um critério ou
  observação invoca. Ganho de comportamento: o fato passa a valer nos **dois**
  sentidos — antes só era capaz de forçar aprovação, agora também reprova
  critério que o LLM aprovou contra a evidência registrada.
- Inversões de camada eliminadas: `app/agents/{judge,planner,advisor}.py`
  deixaram de importar DTOs de `app.graph.nodes`, e
  `app/infrastructure/{settings,workspace_runtime}.py` deixaram de importar de
  `app.agents.executor`. `app/agents/validation.py` virou
  `app/models/validation.py` — era contrato compartilhado morando no pacote
  de agentes.
- A fila PostgreSQL passou de conexão única serializada por lock para
  `psycopg_pool.AsyncConnectionPool`, com validação na retirada. Workers
  concorrentes deixam de disputar o mesmo socket e uma conexão derrubada
  (failover, reboot do banco) não inutiliza mais a fila até o restart.

### Segurança

- O sandbox Docker deixou de executar via `sh -lc`. A allowlist validava só
  `argv[0]` e entregava a string crua ao shell, então `pytest && rm -rf ...`
  passava. Agora o argv validado vai direto ao container, em forma exec, e a
  `CommandPolicy` rejeita operadores e substituições de shell.
  **Quebra de configuração:** comandos de validação que encadeavam etapas numa
  string só (`"ruff check . && mypy app"`) passam a ser rejeitados. Declare uma
  etapa por validador e componha a ordem em
  `OBJECTIVE_VALIDATION_PIPELINES_JSON`.
- O snapshot de `git status`/`git diff` deixou de usar `create_subprocess_shell`
  no host. Ele passa pelo mesmo `CommandRunner` da validação objetiva, então
  com `EXECUTOR_COMMAND_BACKEND=docker` não escapa mais para fora do sandbox.

### Corrigido

- Aprovação do judge passou a respeitar `criteria_ok` também na descida: um
  critério reprovado por fato verificado não é mais sobreposto pela aprovação
  do LLM.
- Corrida em `test_operational_endpoints_expose_health_readiness_and_metrics`:
  o workflow chega a `completed` no checkpoint antes de o worker dar
  acknowledge no job, e a métrica era lida uma única vez.

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
