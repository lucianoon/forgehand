## Context

O worker atual já faz poll/claim na fila (`WorkflowService._worker_loop`) e o sandbox já isola builds (`DockerBuildRunner`). O limite operacional está documentado em `docs/factory-lifecycle.md`: um host, journal e leases no mesmo disco, sem disco efêmero entre hosts. O diagrama Cursor acerta o perímetro (execução na conta do cliente, sempre outbound), mas Lambda MicroVM não cabe no loop LangGraph: timeout, ausência de daemon Docker e sessões longas.

## Goals / Non-Goals

Goals: porto estável, adapter local sem regressão, contratos AWS/GCP executáveis por teste de fake, workspace de sessão no isolador, invariantes de rede/identidade, preflight honesto.

Non-goals: Azure, inferência no isolador, “agnóstico” que esconde o isolador, NFS/EFS como substituto de isolamento, webhook inbound, worker eterno com chave longa, deploy live na conta do usuário, exatamente-uma execução de LLM/SCM, UI nova.

## Decisions

1. **Separar cérebro e mãos, não levantar o grafo na Lambda.**  
   API, checkpointer, fila PostgreSQL e o loop LangGraph ficam no plano de controle durável (host atual, ECS service ou Cloud Run service). Função agendada só acorda o controller: poll + claim + start do isolador. Reentrega, heartbeat e gate humano continuam na fila existente.

2. **Porto com quatro interfaces, um adapter ativo.**  
   `Claimer`, `Isolator`, `Identity`, `Sink`. Configuração `execution_plane: local | aws | gcp`. Misturar papéis de nuvens diferentes na mesma sessão é erro de configuração e falha fechado.

3. **Adapter local é o comportamento atual.**  
   `local` encapsula o host Compose, socket Docker, journal SQLite e lock POSIX. Nenhuma entrega existente muda de caminho sem configuração explícita.

4. **Workspace de nuvem nasce e morre no isolador.**  
   AWS/GCP clonam o SHA pinado para um filesystem da sessão e destroem o ambiente no terminal ou no timeout. O plano de controle guarda journal, evidência e metadados; não monta o data root do host via NFS. Isso remove o veto de “cross-host ephemeral disks” só para esses adapters.

5. **Isolador é o runtime de container da nuvem, não Docker-in-Lambda.**  

   | Papel | AWS | GCP |
   |---|---|---|
   | Despertar | EventBridge | Cloud Scheduler |
   | Controller | Lambda (outbound HTTPS) | Cloud Functions / Cloud Run |
   | Isolador da sessão | ECS Fargate task (primeiro) | Cloud Run Job |
   | Segredo | SSM Parameter Store / Secrets Manager | Secret Manager |
   | Identidade | task role / assume-role | Workload Identity |
   | Logs/artefatos | CloudWatch (+ bucket opcional) | Cloud Logging (+ GCS opcional) |

   Lambda/Functions não executam fases de fábrica nem o grafo. CodeBuild e GCE ficam como alternativa documentada, não como caminho padrão.

6. **Imagens de perfil continuam pinadas por digest.**  
   O isolador de nuvem sobe a mesma imagem aprovada do perfil, sem pull implícito de tag móvel e sem socket Docker do host. Rede do isolador é só a necessária para clone/SCM/LLM/claim; as fases de validação preservam o contrato atual de rede desligada **dentro** do job, ou falham fechado se a nuvem não puder oferecer equivalente.

7. **Identidade curta e sink no perímetro do cliente.**  
   O isolador assume papel; não recebe `AWS_SECRET_ACCESS_KEY` / JSON de service account de longa duração. Controller lê segredo do store e troca por credencial de sessão. Logs e artefatos não voltam como corpo de exceção para o SaaS; o plano de controle recebe referências e trechos já sanitizados.

8. **Preflight não mente.**  
   Com `local`, os checks atuais permanecem. Com `aws`/`gcp`, saúde de fila/worker local não atesta Fargate/Cloud Run, IAM nem Secret Manager. Esses itens entram como `unverified` até o adapter expor atestação explícita; ausência de atestação bloqueia dispatch em `prod`.

## Risks / Trade-offs

- Fargate/Cloud Run ≠ Firecracker Lambda. O isolamento é de task/job, não de MicroVM de função. Documentar a fronteira; não vender “mesmo isolador da Cursor”.
- Sem rede nas fases de validação pode ser mais fraco na nuvem que no Docker local. Se o provedor não permitir equivalente, o adapter recusa o perfil em vez de afrouxar.
- Custo e cold start de uma task por sessão. Lease da fila precisa cobrir provisionamento; timeout do isolador tem de ser menor que o wall clock do workflow.
- Plano de controle ainda vê stream de diffs e prompts. Residência de disco ≠ residência de inferência.
- Dois painéis de debug (Forgehand + CloudWatch/Cloud Logging). O sink deve carregar `workflow_id` e token de ownership em todo log.

## Migration Plan

Só aditivo. Default continua `local`. Operador escolhe `aws` ou `gcp` por instalação, com identidade, fila e revision alinhados. Upgrade conjunto de API e workers, como nas mudanças de resume. Rollback: voltar `execution_plane=local`; não apagar journal nem receipts para “limpar” uma task órfã — o isolador precisa de quarentena/reconcile equivalente à do sandbox atual.

## Open Questions

Nenhuma bloqueia o contrato. A primeira implementação live (conta AWS ou GCP real) fica fora deste change; os testes usam fakes e o esboço de IaC não é pipeline de produção.
