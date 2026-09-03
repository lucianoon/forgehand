## Why

O Forgehand já consegue planejar tarefas, aplicar alterações, executar validações e publicar pull requests, mas essas capacidades dependem de um workspace preparado manualmente e ainda não foram demonstradas em um fluxo de programação real. O MVP deve transformar esse núcleo em uma fábrica operacional e auditável que recebe uma demanda de um repositório existente e devolve um pull request validado, pronto para revisão humana.

## What Changes

- Aceitar uma demanda estruturada ou uma issue do GitHub como ordem de trabalho, preservando origem, critérios de aceite e limites operacionais.
- Preparar automaticamente uma cópia isolada do repositório e uma branch exclusiva para cada workflow, impedindo interferência entre execuções concorrentes.
- Detectar a estratégia de construção do projeto por configuração explícita e convenções seguras, executando instalação, build, testes, lint e análise de tipos em sandbox sem rede por padrão.
- Conectar planejamento, implementação, autocorreção, validação, publicação e acompanhamento do CI em um único ciclo de entrega.
- Exigir aprovação humana antes do merge e oferecer encerramento ou limpeza segura dos recursos temporários.
- Adicionar uma suíte de qualificação com tarefas reais de programação e métricas de PR verde, first pass, custo, duração e intervenção humana.
- Manter o comportamento atual disponível: workflows analíticos e workspaces fornecidos manualmente continuam funcionando.

## Capabilities

### New Capabilities

- `factory-work-order`: Normalização e rastreabilidade de demandas originadas por requisição direta ou issue do GitHub.
- `repository-workspace-provisioning`: Preparação, isolamento, ciclo de vida e limpeza do workspace e da branch de cada workflow.
- `project-build-strategy`: Seleção segura e auditável dos comandos necessários para instalar, construir e validar diferentes projetos.
- `verified-software-delivery`: Execução end-to-end da ordem de trabalho até um pull request validado, com reparo de CI e gate humano.
- `factory-qualification`: Benchmark reproduzível de mudanças reais em repositórios de referência e publicação das métricas de eficácia.

### Modified Capabilities

Nenhuma. O repositório ainda não possui especificações OpenSpec consolidadas; as capacidades atuais relevantes serão formalizadas como parte das novas especificações acima.

## Impact

- APIs de criação, consulta, decisão e cancelamento de workflows.
- Estado do workflow, checkpoints, fila e modelo de auditoria.
- Integrações de grounding, workspace runtime, sandbox e GitHub SCM.
- Configuração de credenciais, políticas de comandos, limites de recursos e retenção de workspaces.
- Dashboard para origem da demanda, preparação do repositório, validações, PR, CI e aprovação.
- Testes unitários e de integração, fixtures de repositórios e workflow de benchmark no GitHub Actions.
- Novas operações de rede e disco exigem controles explícitos contra SSRF, path traversal, vazamento de credenciais e execução de código não confiável.
