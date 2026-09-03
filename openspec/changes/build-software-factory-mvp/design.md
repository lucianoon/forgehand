## Context

O Forgehand possui um grafo durável, executores por capability, operações de arquivo, validação objetiva e publicação de pull requests. Hoje, porém, o `LocalWorkspaceRuntime` é construído sobre uma raiz previamente configurada, o grounding lê outra raiz global e o cliente GitHub entra apenas no final da execução. Isso é suficiente para demonstrações e análises, mas não estabelece uma unidade isolada de produção por demanda.

O MVP atende equipes que mantêm repositórios GitHub e querem automatizar mudanças pequenas e médias, mantendo revisão e merge sob responsabilidade humana. O sistema deve continuar operando localmente e preservar os workflows analíticos existentes.

## Goals / Non-Goals

**Goals:**

- Receber uma solicitação direta ou issue e convertê-la em uma ordem de trabalho rastreável.
- Preparar automaticamente um workspace Git isolado por workflow.
- Executar uma pipeline segura e específica do projeto para construir e validar as mudanças.
- Produzir um único pull request, observar o CI e tentar reparos dentro de limites explícitos.
- Manter checkpoints, auditoria, orçamento, cancelamento e gate humano em todo o fluxo.
- Demonstrar o comportamento com tarefas reais de programação e métricas reproduzíveis.

**Non-Goals:**

- Fazer merge automático, deploy em produção ou administrar ambientes de clientes.
- Criar uma plataforma genérica de CI ou substituir GitHub Actions.
- Permitir que conteúdo do repositório forneça comandos shell arbitrários.
- Suportar todos os provedores SCM ou todas as stacks no primeiro release.
- Prometer entrega autônoma de produtos complexos ou eliminar revisão humana.

## Decisions

### 1. Introduzir uma ordem de trabalho canônica

A API normalizará toda entrada em `WorkOrder`, contendo origem, repositório, ref base, descrição, critérios de aceite, perfil de build, orçamento e política de entrega. Uma issue será lida uma vez no início e armazenada como snapshot imutável com URL, número, título, corpo e timestamp.

Isso evita espalhar regras específicas do GitHub pelo grafo e permite adicionar outras origens depois. A alternativa de fazer o planner consultar a issue diretamente foi rejeitada porque reduz reprodutibilidade e mistura aquisição de requisitos com raciocínio do modelo.

### 2. Adicionar uma fase explícita de provisionamento antes do grounding

O grafo ganhará `provision_workspace` entre a carga de contexto e o planejamento. Um `WorkspaceManager` criará um checkout isolado, fixará o SHA base e devolverá um `WorkspaceLease` persistível com caminhos, branch, commit base e estado do ciclo de vida. Grounding, ferramentas e executor receberão a raiz dessa lease, eliminando raízes globais divergentes.

A primeira implementação usará um cache Git somente leitura por repositório e um worktree ou clone efêmero por workflow. Uma cópia única compartilhada foi rejeitada porque workflows concorrentes poderiam alterar ou validar os arquivos uns dos outros.

### 3. Tornar runtimes dependentes do workflow

O container deixará de injetar uma única instância de workspace runtime no executor. Injetará fábricas capazes de construir grounding, ferramentas e runtime para a `WorkspaceLease` do workflow. O estado persistirá somente identificadores e metadados portáveis; objetos de processo serão reconstruídos pelo worker após restart.

Essa decisão preserva o modelo de checkpoints e permite que outro worker retome o job quando o armazenamento de workspaces for compartilhado. No MVP, retomada em outro host exigirá um diretório compartilhado ou reprovisionamento determinístico a partir do SHA base e das operações registradas.

### 4. Estratégias de build são dados administrados, não shell fornecido pelo repositório

A seleção seguirá esta precedência:

1. perfil nomeado explicitamente na ordem de trabalho;
2. associação administrada por repositório;
3. detecção por arquivos conhecidos, como `pyproject.toml` ou `package.json`;
4. estado `unsupported` quando não houver correspondência segura.

Cada perfil mapeará fases nomeadas (`prepare`, `build`, `test`, `lint`, `types`) para comandos aprovados pelo operador. Arquivos do repositório poderão selecionar capacidades declarativas, mas não introduzir executáveis ou argumentos fora da política. A alternativa de executar scripts descobertos livremente maximiza compatibilidade, porém amplia excessivamente a superfície de supply chain e command injection.

### 5. Factory mode usa sandbox por padrão

Código de terceiros será executado no backend Docker, sem rede por padrão, com CPU, memória, processos, tempo e volume gravável limitados. Credenciais de SCM e LLM permanecem no processo controlador e nunca entram no container. A preparação que necessitar baixar dependências será uma fase separada, opt-in, com cache e política de rede explícita.

Execução local continuará disponível para desenvolvimento e para o modo legado, mas não será o default de ordens de fábrica.

### 6. O pull request é a unidade de entrega

Após as tarefas aprovadas, o Forgehand publicará um commit atômico na branch da lease e abrirá ou atualizará um único PR. Com `wait_for_checks`, CI reprovado reabre apenas as tarefas relacionadas aos arquivos publicados e retorna ao ciclo existente de replan. O PR e o workspace serão idempotentes por workflow.

O MVP termina em `ready_for_human_review`; não fará merge. Essa fronteira mantém a decisão irreversível com a equipe e evita ampliar permissões GitHub antes de existir evidência operacional suficiente.

### 7. Qualificação separa capacidade mecânica de eficácia do agente

Testes unitários e de integração provarão provisionamento, isolamento, política de comandos e publicação. Um benchmark end-to-end separado executará tarefas de programação em repositórios fixture versionados, usando LLM real somente quando explicitamente acionado. O gate inicial será pelo menos 4 de 5 entregas com PR e CI verde, zero violação de isolamento e zero falha técnica não classificada.

## Risks / Trade-offs

- [Código do repositório compromete o host] → sandbox sem rede, limites de recursos, volume mínimo e credenciais fora do container.
- [Instalação de dependências exige rede] → fase separada e opt-in, allowlist de destinos ou cache pré-construído e auditoria do acesso.
- [Workspaces acumulam e consomem disco] → TTL configurável, limpeza idempotente e retenção apenas para falhas ou investigação.
- [Restart em outro worker perde o diretório local] → persistir SHA e operações; exigir storage compartilhado no modo distribuído ou reprovisionar e reaplicar deterministicamente.
- [Detecção escolhe comandos errados] → precedência para perfil explícito, falha segura como `unsupported` e exibição da estratégia antes da execução.
- [LLM produz mudança superficial que passa nos testes] → critérios tipados, judge independente, diff review humano e fixtures com testes ocultos na qualificação.
- [Loop de CI aumenta custo e duração] → limites existentes de custo, tempo e iterações; falha final encaminhada ao gate humano.
- [Escopo inicial favorece GitHub] → interfaces `WorkOrderSource`, `WorkspaceManager` e `DeliveryPublisher` mantêm a extensão futura sem generalizar o MVP prematuramente.

## Migration Plan

1. Introduzir modelos e interfaces atrás de `FACTORY_MODE_ENABLED=false`, sem alterar workflows existentes.
2. Implementar provisionamento local e estratégias para Python e Node em testes com repositórios fixture.
3. Integrar o novo subgrafo com API e dashboard, mantendo o caminho legado quando não houver `work_order`.
4. Executar testes de isolamento, restart e segurança; habilitar factory mode somente em ambiente de piloto.
5. Rodar o benchmark end-to-end e liberar para design partner após atingir o gate.

Rollback: desabilitar `FACTORY_MODE_ENABLED`; ordens novas voltam ao fluxo legado. Workspaces e branches já criados permanecem rastreáveis e são limpos pelo reconciliador sem excluir branches remotas ou PRs.

## Open Questions

- Qual storage compartilhado será adotado quando múltiplos workers rodarem em hosts diferentes?
- A fase de preparação de dependências usará proxy allowlisted, imagens pré-aquecidas ou ambos no ambiente de piloto?
- Quais dois repositórios fixture representarão melhor o primeiro design partner além das stacks Python e Node básicas?
