## Why

A fábrica hoje assume um host Linux, disco local compartilhado e socket Docker. Isso impede residência de execução na conta do cliente e trava o Forgehand no modelo “um controlador, um filesystem”. O padrão pull-based do diagrama Cursor (cérebro separado das mãos, claim de saída, sandbox efêmero, identidade curta) precisa virar um plano de execução com adapters AWS e GCP, sem reescrever o loop do agente.

## What Changes

- Extrair um porto `ExecutionPlane` com quatro papéis estáveis: claimer, isolator, identity e sink.
- Manter o host atual como adapter `local` (Compose de equipe e sandbox Docker sem rede continuam o padrão).
- Definir adapters `aws` e `gcp` que clonam o workspace **dentro** do isolador da sessão, sem NFS nem disco cruzado entre hosts.
- Usar função agendada só como controller (poll + claim + disparo). O grafo LangGraph, checkpoints e lease da fila permanecem num plano de controle durável.
- Exigir saída HTTPS, segredo fora do runtime e credencial de curta duração. Recusar inbound e chave longa no isolador.
- Estender o preflight para declarar condições remotas como não verificadas até haver atestação do adapter.

## Capabilities

### New Capabilities

- `cloud-execution-plane`: Contrato agnóstico do plano de execução e mapeamento explícito dos adapters AWS e GCP.

### Modified Capabilities

- `delivery-execution-preflight`: o relatório passa a distinguir saúde local de atestação remota do plano de execução.
- `repository-workspace-provisioning`: adapters de nuvem usam checkout efêmero por sessão no isolador, não o data root compartilhado do host.

## Impact

Porto de execução, seleção de adapter por configuração, preflight, ciclo de vida do workspace em nuvem, testes de contrato com fakes, e documentação operacional. Sem deploy live obrigatório, sem Azure, sem mover inferência para o isolador, sem abandonar o Compose de equipe. Terraform/SAM/gcloud entram como esboço versionado, não como pipeline de produção neste change.
