# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/);
versionamento segue [SemVer](https://semver.org/lang/pt-BR/).

## [Não publicado]

### Adicionado

- Avaliação contínua (`app/evaluation/evals.py`, `evals/cases.json`,
  `evals/gates.json`, workflow `Evals`): casos reais com LLM, orçamento total
  em dólares como teto duro, relatório JSON + Markdown e gate por conclusão,
  first pass, custo médio e p95. Linha de base em `evals/baseline/`.
- Ferramentas MCP por stdio (`MCP_SERVERS_JSON`, `MCP_TOOLS_ROLES`): servidores
  do operador viram `mcp_<servidor>_<ferramenta>` no mesmo ToolLoop, com
  hooks, tetos, allowlist por ferramenta e ambiente sem segredos.
- CLI `forgehand` (run, status, decide, cancel) contra a API em execução.
- `GET /workflows/{id}/events` (SSE) e dashboard consumindo o fluxo, com
  polling como fallback.
- `forgehand.toml` (ou `FORGEHAND_CONFIG`) como fonte de configuração abaixo
  das variáveis de ambiente; `docs/quickstart.md` do zero ao primeiro workflow.
- `PLANNER_TIER`/`JUDGE_TIER` e escalonamento do planner um tier acima quando
  a validação estrutural rejeita o plano; memória de projeto injeta lições
  (reprovações do judge por capability) no contexto do planner.
- Corpo do PR traz a tabela de critérios verificados por tarefa (nota e quem
  validou: código, pytest, ruff, mypy ou judge).
- `op=replace` tolera diferença de indentação entre o trecho copiado e o
  arquivo e reindenta a substituição; palavras do pedido são normalizadas
  (acentos, plural, sufixos) antes de casar com o repositório.
- Em `ENVIRONMENT=prod`, comandos do executor exigem backend Docker;
  autocorreção passa a uma rodada por padrão (ciclo dirigido por teste).
- Critérios objetivos para tarefas que não gravam arquivo: `output_contains`
  (regex sobre summary/notes) e `output_min_chars` (tamanho mínimo do texto
  entregue). Análise, pesquisa e síntese deixam de depender só do judge LLM;
  o planner é instruído a usá-los em capabilities sem escrita.
- Ferramenta `run_command` para o executor (`AGENT_TOOLS_ALLOW_COMMANDS`,
  opt-in): comandos da allowlist do `CommandPolicy` sem shell, com subcomandos
  de rede e instalação negados, timeout configurável e ambiente sem segredos
  no backend local; passa pelos hooks e tetos como as demais ferramentas.
- O runner local de comandos aceita timeout (mata o grupo de processos) e
  ambiente saneado; cada chamada de LLM registra em log tokens de entrada,
  cache (escrita/leitura), saída, custo e latência.
- Ferramenta `fetch_url` para planner e executor (`AGENT_WEB_FETCH_ENABLED`,
  `AGENT_WEB_FETCH_ROLES`; opt-in): o agente busca uma página no meio da
  tarefa com as mesmas guardas e limites das referências web, o texto volta
  marcado como externo e não confiável, e a chamada passa pelos hooks e
  pelos tetos do papel como qualquer outra ferramenta.
- Referências web na solicitação (`WEB_REFERENCES_ENABLED`, opt-in): URLs do
  pedido são buscadas uma vez pelo controlador e viram evidências `[W1]`,
  `[W2]`... no mesmo circuito de citações do grounding do repositório, com
  guarda anti-SSRF (resolução prévia, endereços não públicos recusados a cada
  redirecionamento), allowlist por sufixo de host, limites de bytes e
  caracteres e só content-type textual. HTML vira texto preferindo
  `<main>`/`<article>` e descartando `nav`/`footer`. `WEB_REFERENCES_CA_BUNDLE`
  soma um PEM corporativo ao certifi. O sandbox continua sem rede. Validado ao
  vivo: guia gerado a partir da documentação do uv, citando `[W1]`, aprovado
  na primeira tentativa.

### Alterado

- Grounding do repositório por relevância: arquivos sem nenhuma palavra do
  pedido no caminho ou no texto ficam de fora, salvo referências do projeto
  (README, pyproject...), e `REPOSITORY_GROUNDING_MAX_TOTAL_CHARS` (40 mil)
  limita o prefixo cacheado enviado a planner, executor e judge. Na primeira
  rodada real o planner recebia 16 arquivos irrelevantes (~9 mil tokens) para
  um pedido sem relação com o repositório. `REPOSITORY_GROUNDING_REQUIRE_KEYWORD_MATCH=false`
  restaura o comportamento anterior.

### Corrigido

- Decisões reenviadas distinguem interrupts sequenciais dentro do mesmo nó:
  o envelope v2 inclui checkpoint, task e posição persistida da aprovação.
  IDs sozinhos podem ser reutilizados pelo LangGraph; envelopes antigos
  ambíguos preservam o gate e pedem uma decisão nova. Posições de subgrafos
  não resolvidas são recusadas, sem assumir que representam o primeiro gate.

- Qualificação independente passa a conferir a identidade completa do PR antes
  e depois da execução, verificar ancestralidade da base e inventariar o diff
  diretamente dos commits publicados. Registra SHA verificado e hashes do
  verificador/perfil; saída zero sem conclusão explícita não comprova sucesso.
- CI pagina todos os check runs e statuses atuais até o limite documentado,
  rejeitando inventários incompletos ou inconsistentes. A qualificação exige
  pelo menos um check realmente concluído com sucesso e relê CI após o build.
- Verificadores Python/Node ampliam casos de borda e preservação de comportamento;
  controles locais executam implementações corretas e defeitos conhecidos em
  processos reais, sem LLM. As tarefas que pedem regressões passam a verificar
  que os testes entregues detectam uma mutação do comportamento solicitado.
  Os novos critérios ainda exigem uma rodada completa de qualificação com LLM.

- Workers interrompidos no shutdown não confirmam jobs incompletos. Após perda
  de processo/lease, jobs `start` continuam o checkpoint pendente sem reenviar
  o pedido inicial; conclusões e gates já persistidos não são reexecutados.
- Decisões humanas na fila passam a carregar os IDs dos interrupts aprovados:
  uma reentrega não reutiliza uma aprovação num gate posterior. Mensagens
  legadas ambíguas ou malformadas preservam o checkpoint e exigem nova decisão.
  API e workers devem ser atualizados juntos (ver `docs/worker-recovery.md`).
- Testes PostgreSQL incluem SIGKILL em processos reais, antes do primeiro
  checkpoint, durante execução e entre conclusão/gate e confirmação na fila.
  O antigo teste de restart foi descrito corretamente como recriação de
  componentes no mesmo processo.

- Grounding e busca passam a descobrir fontes JavaScript/TypeScript, inclusive
  `.cjs` e `.mjs`: os fixtures Node antes forneciam README sem implementação
  ou testes. Em autocorreções e novas tentativas, o executor relê até quatro
  arquivos envolvidos via `read_file`, priorizando operações que falharam e
  arquivos recentes. As leituras respeitam hooks, confinamento e teto de calls.
  Regressão com Node real cobre correção após teste com `expect` indisponível;
  isso valida o contexto de recuperação, não uma nova qualificação com LLM.

- Benchmark: first pass exige conclusão bem-sucedida, inclusive na agregação
  de resultados importados. O custo por conclusão inclui o gasto de todos os
  casos e passa a ser `null` quando não há conclusões (antes era zero).
  Uma suíte vazia sempre reprova o gate, mesmo com limites de taxa zero.

- Evidência cumulativa da autocorreção passa a ser relativa ao início da
  tarefa: arquivo criado na rodada 1 e editado na rodada 2 continua `created`
  (antes virava `modified` e reprovava `file_created`); criado e removido na
  mesma tarefa sai de tudo, inclusive de `deleted_paths`. Achado da primeira
  rodada de evals.
- O allowlist do checkpoint não declarava `AcceptanceCriterion` nem
  `CriterionKind`: cada retomada de estado registrava "Blocked deserialization
  of app.models.task.CriterionKind". Teste de ida e volta incluído.
- Sob o uvicorn nenhum log do aplicativo abaixo de WARNING aparecia (o raiz
  não tinha handler): `configure_logging()` na API e no worker, nível por
  `LOG_LEVEL`, sem sobrescrever configuração existente.
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
