# Resumo executivo do Agent Forge

## Visão executiva da arquitetura atual

O repositório declara um sistema multiagente de desenvolvimento de software. Nos trechos observados, a composição de dependências referencia uma fronteira HTTP-grafo, planner, judge, registro de executores, workflow, memória de projeto, coleta de grounding e providers de modelos. **Arquivos-chave:** `README.md`, `app/api/dependencies.py` — **Evidências:** `E2`, `E8`.

O planner declara a transformação de requisições em planos de `AgentTask`, com critérios de aceitação, evidências e dependências; ele importa grounding e roteamento por provider/tier. O judge declara avaliação por critério, mudanças exigidas e aprovação condicionada à decisão do modelo e aos sinais objetivos. **Arquivos-chave:** `app/agents/planner.py`, `app/agents/judge.py` — **Evidências:** `E1`, `E5`.

`app/agents/grounding.py` lê o grounding do contexto, indexa evidências por ID, normaliza citações e inicia sua validação. A coleta de grounding é apenas referenciada pela composição por meio de `RepositoryGroundingCollector`; seu algoritmo não aparece nos trechos. **Arquivos-chave:** `app/agents/grounding.py`, `app/api/dependencies.py` — **Evidências:** `E6`, `E8`.

`WorkflowService` é descrito como a fronteira entre HTTP e o grafo: sua docstring declara início assíncrono, leitura pelo checkpointer e retomada de decisões com `Command(resume=...)`. O teste de API importa os routers de operações e workflows e declara um cenário HTTP -> grafo -> checkpointer -> HTTP; o trecho não demonstra chamadas a `include_router`. **Arquivos-chave:** `app/api/dependencies.py`, `tests/integration/test_api.py`, `app/api/routes/operations.py`, `app/api/routes/workflows.py` — **Evidências:** `E8`, `E7`.

Planner e judge importam `ProviderRouter` e `ModelTier`. A composição importa providers Anthropic e OpenAI-compatible, enquanto o README declara OpenRouter, por meio do provider OpenAI-compatible, como caminho recomendado e Anthropic como alternativa. **Arquivos-chave:** `app/agents/planner.py`, `app/agents/judge.py`, `app/api/dependencies.py`, `README.md` — **Evidências:** `E1`, `E5`, `E8`, `E2`.

### Módulos e arquivos-chave

| Área | Arquivos observados | Papel demonstrado ou declarado | Evidências |
|---|---|---|---|
| Planejamento | `app/agents/planner.py` | Declara a produção de planos de tarefas com critérios, evidências e dependências; importa abstrações de grounding e provider. | `E1` |
| Grounding | `app/agents/grounding.py` | Lê grounding do contexto, indexa evidências, determina exigência de citações, normaliza citações e inicia sua validação. | `E6` |
| Julgamento | `app/agents/judge.py` | Declara avaliação por critério e combinação entre decisão do LLM e sinais objetivos. | `E5` |
| Composição e serviço | `app/api/dependencies.py` | Importa e reúne referências a agentes, workflow, infraestrutura e providers; declara `WorkflowService` como fronteira HTTP-grafo. | `E8` |
| Workflow e estado | `app/graph/workflow.py`, `app/graph/state.py` | São importados pela composição e pelos testes; seus trechos internos não foram fornecidos. | `E3`, `E4`, `E7`, `E8` |
| Providers | `app/providers/registry.py`, `app/providers/anthropic_provider.py`, `app/providers/openai_compatible.py` | São referenciados como abstrações ou implementações disponíveis na composição e nos cenários observados. | `E1`, `E4`, `E5`, `E8` |
| Fronteira HTTP | `app/api/routes/operations.py`, `app/api/routes/workflows.py`, `tests/integration/test_api.py` | E7 importa os routers e declara um cenário HTTP -> grafo -> checkpointer -> HTTP. | `E7` |
| Testes | `tests/unit/test_workflow_regressions.py`, `tests/integration/test_agents_e2e.py`, `tests/integration/test_api.py` | Expõem doubles, dependências e cenários declarados de workflow, agentes e API. | `E3`, `E4`, `E7` |

## 5 achados técnicos objetivos

1. **O planejamento possui contratos explícitos para tarefas e dependências.** O planner declara tarefas com critérios de aceitação, IDs de evidência e dependências por índice; sua docstring descreve conversão para UUID, validação de faixa e detecção de ciclos por Kahn. O trecho também importa `ProviderRouter`, `ModelTier` e funções de grounding. **Arquivo relacionado:** `app/agents/planner.py` — **Evidência:** `E1`.

2. **O julgamento combina avaliação do modelo, sinais objetivos e regras de citação.** O judge declara veredictos por critério, falhas, mudanças exigidas, score e aprovação; sua docstring descreve o veto estrutural entre aprovação do LLM e sinais objetivos. O arquivo importa normalização e validação de citações, mas também informa que a lista de validadores objetivos pode estar vazia. **Arquivo relacionado:** `app/agents/judge.py` — **Evidência:** `E5`.

3. **A camada de grounding observada valida a estrutura e as referências das citações.** O módulo lê `context["repository_grounding"]`, aceita grounding com lista não vazia de evidências, constrói índice por ID, determina se citações são obrigatórias, remove duplicatas e inicia a detecção de citações ausentes ou desconhecidas. O trecho não demonstra coleta de arquivos. **Arquivo relacionado:** `app/agents/grounding.py` — **Evidência:** `E6`.

4. **A composição declara uma fronteira de workflow orientada a checkpoint e retomada.** `WorkflowService` é descrito como fronteira entre HTTP e grafo, com `start()` disparando `ainvoke` em background, `get()` lendo do checkpointer e `decide()` retomando por `Command(resume=...)`. A mesma composição importa planner, judge, registro de executores, workflow, auditoria, grounding e providers; isso comprova referências estáticas, não a execução bem-sucedida do fluxo. **Arquivo relacionado:** `app/api/dependencies.py` — **Evidência:** `E8`.

5. **Foram observados módulos de teste unitário e de integração, sem comprovação de cobertura ou sucesso da suíte.** E4 e E7 declaram em suas docstrings cenários de grafo com agentes e de HTTP -> grafo -> checkpointer -> HTTP, enquanto E3 expõe dependências e doubles de teste para workflow e judge. Não há resultados de execução, métricas de cobertura ou confirmação de aprovação. **Arquivos relacionados:** `tests/unit/test_workflow_regressions.py`, `tests/integration/test_agents_e2e.py`, `tests/integration/test_api.py` — **Evidências:** `E3`, `E4`, `E7`.

## 3 próximos passos prioritários

1. **Executar e registrar a suíte observada, incluindo resultados e cobertura.** Priorizar os módulos unitário e de integração já identificados e publicar os comandos, resultados, falhas e métricas obtidas. **Motivação:** E3, E4 e E7 demonstram arquivos, doubles e cenários declarados, mas não fornecem resultados de execução nem cobertura comprovada. **Arquivos de referência:** `tests/unit/test_workflow_regressions.py`, `tests/integration/test_agents_e2e.py`, `tests/integration/test_api.py` — **Evidências:** `E3`, `E4`, `E7`.

2. **Documentar e verificar o fluxo interno do workflow e os contratos HTTP.** Levantar nós, transições, retries e condições do grafo, além de endpoints, métodos, schemas e vínculo efetivo dos routers, sem pressupor esses detalhes antes da inspeção. **Motivação:** `build_workflow` é apenas importado pelos trechos disponíveis, e E7 importa os routers e declara o cenário HTTP -> grafo -> checkpointer, mas não mostra a implementação do grafo, os contratos das rotas ou chamadas a `include_router`. **Arquivos de referência:** `app/graph/workflow.py`, `app/api/routes/operations.py`, `app/api/routes/workflows.py`, `tests/integration/test_api.py`, `app/api/dependencies.py` — **Evidências:** `E3`, `E4`, `E7`, `E8`.

3. **Validar operacionalmente os modos de provider, persistência e worker documentados.** Criar evidência executável para as configurações OpenRouter/Anthropic e para o modo com checkpointer e fila Postgres, registrando comportamento, falhas e requisitos reais. **Motivação:** o README declara esses modos e a composição importa providers e infraestrutura relacionada, mas os trechos não mostram a implementação interna dos providers, do backend Postgres ou do worker. **Arquivos de referência:** `README.md`, `app/api/dependencies.py`, `app/providers/anthropic_provider.py`, `app/providers/openai_compatible.py` — **Evidências:** `E2`, `E8`.

## Limites deste resumo

As relações acima derivam somente de imports, docstrings, prompts, declarações do README e cenários de teste visíveis em E1-E8. Imports demonstram referências estáticas, e docstrings ou README demonstram contratos e modos declarados; isoladamente, eles não comprovam execução bem-sucedida. Não foram inferidos endpoints, implementação interna do grafo, algoritmo de coleta de grounding, detalhes dos providers, backend Postgres, worker, resultados de testes ou cobertura além do que aparece nessas evidências. **Evidências:** `E1`, `E2`, `E3`, `E4`, `E5`, `E6`, `E7`, `E8`.
