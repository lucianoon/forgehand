# Recuperação confiável de envio

O Forgehand aceita novos workflows em uma operação atômica: chave de idempotência,
recibo de admissão e job são gravados juntos. Repetir a mesma solicitação devolve
o workflow original, sem criar outra entrada de início, mesmo se ele já terminou,
falhou ou foi cancelado. Conteúdo/projeto conflitante retorna `409`, não um falso
sucesso. IDs e timestamps gerados para a WorkOrder não alteram a identidade lógica;
requisitos, destino, critérios, orçamento e configuração de entrega alteram.

Isso é **admissão idempotente**, não execução exatamente uma vez. Leases podem
permitir reentrega de jobs; chamadas de IA e efeitos remotos no GitHub continuam
exigindo controles próprios. Recibo de admissão não comprova entrega concluída,
qualidade, merge nem gasto zero em uma repetição executada pelo worker.

## O que fica persistido

- SQLite: plano, tentativa, contexto/ordem original e intenção de envio são
  confirmados na mesma transação. A intenção contém a identidade da fila e o
  hash da ordem aprovada, nunca credenciais.
- PostgreSQL: `workflow_dispatch_identity`, `workflow_start_receipts`,
  `workflow_idempotency` e `workflow_jobs`. Chave, recibo e job de início são
  confirmados na mesma transação; erro/cancelamento antes do commit desfaz tudo.
- A intenção SQLite é recuperável por ação explícita; não há despachante automático.
  Os dois bancos continuam separados. O Studio ainda é single-host.

Uma fila em memória tem identidade exclusiva por instância: pode recuperar uma
falha de envio enquanto essa instância existir, mas **não sobrevive a reinícios**.
Para recuperação após reinício, configure `WORKFLOW_QUEUE_BACKEND=postgres` e
`CHECKPOINTER_BACKEND=postgres`, com armazenamento de workspace persistente e os
workers configurados conforme o restante da instalação.

## Procedimento de recuperação

1. Consulte o plano e use `POST /products/{product_id}/delivery/reconcile`. Essa
   operação só consulta evidências; não reenvia nem reinicia trabalho.
2. Se a tentativa atual permanecer `dispatching` ou `dispatch_unknown`, confira
   o workflow, o contexto em `GET /context/{workflow_id}` e o orçamento salvo.
3. Um aprovador do mesmo proprietário/projeto pode usar
   `POST /products/{product_id}/delivery/recover`, com o seguinte corpo:

   ```json
   {
     "revision": 3,
     "workflow_id": "00000000-0000-4000-8000-000000000001",
     "approved": true
   }
   ```

   Substitua a revisão e o ID pelos valores atuais do plano. Autentique com a
   chave do Forgehand via `X-API-Key`; nunca envie a chave do provedor no corpo.
   A operação também pode ser chamada pela documentação interativa em `/docs`.
4. A API revalida fábrica habilitada, credencial configurada, perfil/política de
   sandbox, fila e workers. O ID deve ser o da tentativa incerta atual. Não é
   permitido enviar novos requisitos, orçamento ou tokens nesse endpoint.
5. A recuperação usa **a mesma ordem, contexto, SHA base, workflow e limites**.
   Se o envio anterior já foi admitido, a fila não insere outro job. Se não foi,
   insere exatamente esse início. Não consome uma nova tentativa do limite de três.
6. Confira o plano devolvido e reconcilie novamente. `202` com
   `dispatch_unknown` significa que a admissão continua sem confirmação — não é
   sucesso da entrega. `running` após a recuperação indica admissão confirmada;
   a reconciliação traz o estado efetivo da execução.

Há auditoria `product_delivery_recover` antes do envio e ao processar a resposta.
Se uma resposta/auditoria final se perder após a admissão, consulte/reconcilie:
não apague registros nem invente uma nova chave para forçar outra execução.
Nenhum GET, atualização visual ou reconciliação dispara a recuperação.
Este marco oferece recuperação pela API; o Studio ainda não tem botão dedicado.

## Bloqueios intencionais

- Tentativas anteriores a esta mudança não têm a intenção versionada necessária:
  continuam conciliáveis, mas exigem investigação para recuperação de envio.
- Chaves legadas criadas pelo antigo protocolo sem recibo atômico não são
  consideradas prova de admissão. Não há backfill automático nem reenvio cego.
- Uma fila nova ou cuja identidade foi rotacionada bloqueia intenções anteriores.
- Ordem salva inválida, hash divergente, revisão desatualizada, outro workflow,
  falta de aprovação, outro proprietário/projeto e execução já reconciliada como
  terminal bloqueiam a recuperação.
- O SHA base não é atualizado na recuperação. Se ficou incompatível, a validação
  normal do workspace deve bloquear; recuperar não autoriza mudar o escopo.
- A checagem de pré-requisitos não testa saldo da IA, permissão remota de escrita
  nem disponibilidade real do Docker. Esses limites do preflight permanecem.

## Atualização, backup e restauração

1. Pause novas admissões/recuperações e pare os workers de forma controlada.
2. Faça backup coordenado do SQLite do Studio, PostgreSQL (fila, recibos,
   idempotência, identidade e checkpoints) e armazenamento dos workspaces.
3. Atualize API e workers para a mesma versão; inicialize as tabelas aditivas.
   Nenhum produto ou tentativa existente é apagado. Evite versões antigas e novas
   admitindo workflows simultaneamente.
4. Verifique saúde e testes operacionais antes de reabrir o tráfego.

**Não exclua recibos, chaves ou identidade para liberar uma tentativa.** O sistema
não remove esses recibos automaticamente. Uma política futura de retenção precisa
preservar a deduplicação por todo o período em que uma intenção possa reaparecer.

Uma restauração de snapshot antigo pode perder jobs/recibos mantendo a identidade
antiga. O UUID, sozinho, não detecta essa regressão. Após restauração, mantenha
admissões e workers parados, rotacione `workflow_dispatch_identity.namespace` para
um UUID novo e investigue efeitos remotos antes de retomar. Isso bloqueia
recuperação das intenções antigas; não substitui reconciliação operacional nem
interrompe automaticamente jobs que já estavam no snapshot restaurado.

Rollback de código também exige pausa e preservação dessas tabelas. Não habilite
o protocolo antigo de admissão concorrente com o novo. Esta mudança não migra o
Studio inteiro para PostgreSQL nem torna o workspace automaticamente multi-host.

## Verificação

`tests/integration/test_dispatch_recovery.py` cobre concorrência, identidade,
integridade, autorização, rollback SQLite/PostgreSQL, cancelamento durante a
transação, perda da confirmação e reconstrução de serviço/conexão. Use
`RUN_POSTGRES_TESTS=1` e `TEST_DATABASE_URL` para executar o backend real em banco
de teste. Cada caso PostgreSQL cria/remove somente seu schema exclusivo.
Os testes não chamam modelos, executam repositórios nem criam PRs.

Validação local em 2026-09-03: **702 testes Python passaram**, com PostgreSQL 16
em container temporário isolado; 25 casos foram pulados (serviços/modos opcionais
e três variantes transacionais não aplicáveis à fila em memória). A bateria
específica desta mudança passou em 37 casos. Também passaram 14 testes JavaScript,
Ruff, mypy e validação estrita do OpenSpec. Isso não é teste de carga, desastre
completo de infraestrutura nem certificação de produção.
