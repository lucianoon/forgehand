# Evolução incremental de produtos

O Studio pode ligar um produto aprovado a um **repositório GitHub existente** e
executar uma funcionalidade por vez pela fábrica. O plano preserva decisões,
critérios de aceitação, regras de preservação e o contexto exato de cada tentativa.
Salvar ou consultar um plano não chama IA, não cria repositório e não publica PR.

## Operação no Studio

1. Abra um produto em `ready_for_preview`. Informe o repositório `owner/repo`,
   branch base e, se necessário, um perfil de build aprovado.
2. Revise o JSON de entregas com critérios concretos. O Studio bloqueia seus
   próprios critérios provisórios até serem substituídos; isso não certifica a
   qualidade dos critérios. Registre decisões e o que não pode ser perdido.
3. Salve o plano. Destino e critérios existentes ficam preservados; novas
   funcionalidades e decisões podem ser acrescentadas quando não há entrega ativa.
4. Confira destino, orçamento **por tentativa** e autorização explícita antes de
   executar. A fábrica usa seus mecanismos existentes de sandbox, hooks, limites,
   revisão, evidências de build e CI. Decisões pendentes aparecem no dashboard.
5. Revise o PR no GitHub e decida seu merge. Depois use **Verificar execução e merge**.
   Apenas um merge confirmado libera a funcionalidade seguinte, que também exige
   autorização. Não há execução, polling, merge ou deploy automático nesta interface.

### Checagem antes de executar

O botão **Checar condições de execução** consulta o plano e a configuração local,
sem iniciar IA, acessar GitHub ou criar containers. Ele verifica elegibilidade da
entrega, limite de tentativas/contexto, política da fábrica, presença de credencial,
perfil de build e disponibilidade da fila/workers. Erros ou timeout de dois segundos
na consulta de saúde bloqueiam o início com uma mensagem sanitizada.

O servidor repete essa checagem no `POST /start`, antes de acessar SCM ou reservar
tentativa. Um relatório anterior positivo não é autorização e não pode contornar
uma fila indisponível ou configuração alterada. Bloqueios retornam HTTP 409 com
`detail.code=delivery_preflight_blocked` e `detail.preflight`. Nenhuma tentativa é
consumida por esse bloqueio. O relatório traz produto/revisão e não é armazenado.

O perfil explícito prevalece sobre o mapeado. Sem ambos, candidatos aprovados para
detecção automática produzem aviso: só o checkout pode confirmar compatibilidade.
A checagem não testa Docker/imagens, saldo do provedor, permissões remotas nem a
configuração efetiva de workers externos. Presença de chave não significa chave
válida. Um heartbeat não certifica capacidade de build. A fila em memória precisa
de workers internos; workers externos exigem a fila PostgreSQL suportada.

Após atualizar o código, reinicie a API para carregar a nova rota e o bloqueio de
execução. Recarregar apenas os arquivos JavaScript não atualiza um processo Python
que já estava em execução.

Se a origem for um ZIP exportado, o código precisa primeiro ser colocado no
repositório por um fluxo separado e autorizado. Este recurso não importa o ZIP,
não cria esse repositório nem garante que ele corresponda à demo.

## Configuração e persistência

O Studio precisa de `PRODUCT_STUDIO_ENABLED=true` e de seu banco
`PRODUCT_STUDIO_DATABASE`. As tabelas `product_delivery_plans` e
`product_delivery_attempts` são adicionadas ao mesmo SQLite, sem apagar produtos.
Mantenha o arquivo em volume persistente e inclua-o nos backups. O Studio continua
sendo single-host: esta mudança não transforma seu armazenamento em serviço
distribuído. A durabilidade de workflows/fila depende da configuração existente;
use os backends PostgreSQL suportados para sobreviver a reinícios do executor.

Salvar planos funciona com a fábrica desativada. Executar exige
`FACTORY_MODE_ENABLED=true`, `github.com` nos hosts aprovados, credencial GitHub
configurada no servidor e infraestrutura/perfil de build operacional. Consulte
[perfis de build](factory-build-profiles.md), [ciclo de vida](factory-lifecycle.md)
e [entrega verificada](factory-delivery.md). Não coloque chaves no briefing,
critérios ou decisões: esses conteúdos persistem e são enviados ao workflow.

As consultas verificam proprietário e projeto. Mutações exigem `approver` e
registram auditoria. O destino utiliza o escopo da credencial GitHub configurada;
esta etapa não acrescenta uma ACL por repositório. Use credenciais restritas aos
destinos autorizados. A chave digitada no Studio é do Forgehand, não da OpenAI.

## Evidência e contexto

O estado `ready_for_human_review` com CI verde não significa merge. A reconciliação
exige o PR do workflow, commit head exato, repositório/branch base correspondentes,
`merged=true` e SHA de merge incorporado à branch base. A comparação GitHub deve
ser `ahead` ou `identical`. Antes da próxima entrega, a base é resolvida e fixada
novamente, preservando a ancestralidade do último merge confirmado. Consulte os
contratos oficiais de [pull requests](https://docs.github.com/en/rest/pulls/pulls)
e [comparação de commits](https://docs.github.com/en/rest/commits/commits#compare-two-commits).

O contexto por tentativa inclui briefing histórico, decisões aprovadas, regras de
preservação, critérios da funcionalidade atual e recibos das entregas já mescladas.
As funcionalidades futuras entram somente como títulos. O contexto e a ordem de
trabalho são gravados antes do envio; o endpoint de contexto devolve o documento
e seu SHA-256. Não é uma memória sem limite nem compactação semântica automática.
O briefing da demo é histórico: suas restrições não substituem o escopo aprovado
para a evolução do repositório.

## Concorrência, falhas e limites

- Revisões e transação SQLite serializam o início da entrega. Um clique/reenvio
  com revisão antiga é rejeitado; respostas atrasadas não regridem merges salvos.
- Se o envio falhar depois de registrar a intenção, a tentativa fica incerta.
  Use a reconciliação para procurar o mesmo workflow; nunca há reenvio automático.
  Novas tentativas com intenção persistida podem usar a
  [recuperação explicitamente aprovada](delivery-recovery.md) na mesma fila:
  chave, recibo e job são admitidos atomicamente, sem alocar outra tentativa.
  Registros legados e filas substituídas exigem investigação operacional.
  Não existe reset forçado nem garantia de execução exactly-once; não apague
  registros para tentar novamente às cegas.
- Falha ou cancelamento confirmado permite nova tentativa explicitamente
  autorizada, com outro workflow e contexto preservado. Verifique efeitos remotos
  da tentativa anterior: uma nova execução pode produzir outro PR.
- Limites: 20 funcionalidades, 20 decisões, 12 regras de preservação, três
  tentativas por funcionalidade e 48 KB de contexto. Contexto excessivo é recusado,
  não truncado. Planos não têm edição destrutiva/reordenação neste primeiro marco.
- Cada tentativa aceita US$ 0,01–5 de orçamento estimado, 1.000–500.000 tokens
  (padrão 100.000), três iterações e 1.800 segundos. O orçamento não é agregado ao
  da demo nem às outras tentativas. Configure também limites no provedor;
  estimativas não são garantia de teto de cobrança.
- Regras de preservação viram requisitos da entrega, não certificação automática
  de migração. Merge comprova incorporação de código, não robustez, segurança,
  desempenho ou correção das regras de negócio. Teste essas propriedades no produto.

## API

Todos os caminhos abaixo usam o prefixo `/products/{product_id}/delivery` e
`X-API-Key`. Os contratos completos estão em `/docs`.

| Método/caminho | Efeito |
|---|---|
| `GET /` | Plano atual, ou `null`, e configuração da fábrica; nunca executa |
| `GET /preflight` | Checagem local sem execução; exige `approver`, retorna `Cache-Control: no-store` |
| `PUT /` | Cria plano; repetir o mesmo conteúdo é idempotente |
| `POST /append` | Acrescenta entregas/decisões com `revision` atual |
| `POST /start` | Autoriza uma tentativa com `revision`, `approved: true`, `max_cost_usd` |
| `POST /recover` | Recupera somente o envio da tentativa incerta atual, com `revision`, `workflow_id` e `approved: true`; usa contexto/orçamento salvos |
| `POST /reconcile` | Consulta evidência e atualiza o histórico; não faz merge |
| `GET /context/{workflow_id}` | Contexto imutável da tentativa desse proprietário |

Os endpoints de raiz não precisam de barra final. Conflitos de revisão, estado,
limites ou verificação de SCM retornam 409; acesso a outro proprietário retorna
404. IDs de workflow são gerados pelo servidor; na recuperação o cliente confirma
o ID existente, sem escolher um novo. Veja [ativação e recuperação](delivery-recovery.md).

## Validação deste marco

Testes cobrem persistência/reinício, concorrência, repetição, isolamento por
proprietário/projeto, autorização, envio incerto, contexto exato, retries explícitos,
evidência de CI e verificação GitHub simulada de head/base/ancestralidade.
Testes JavaScript cobrem renderização como texto, critérios provisórios e execução
explicitamente autorizada. A prévia local usa dados fictícios e fábrica desativada.
Esta validação não executou IA paga, não escreveu no GitHub nem validou uma
migração real ou carga de produção.
