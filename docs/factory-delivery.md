# Entrega validada da fábrica

O workflow de fábrica publica pela branch `forgehand/<workflow-id>` da lease.
O commit inicial usa o SHA base fixado no provisionamento, não a ponta atual
da branch base. O PR continua direcionado à branch declarada na ordem de
trabalho, para revisão e merge humano no GitHub.

## Condições antes da publicação

- Todas as tarefas precisam estar aprovadas.
- A última tentativa de cada tarefa precisa conter evidência de sucesso para
  todas as fases selecionadas, na mesma ordem e com o mesmo fingerprint.
- Saída diferente de zero, fase ausente, erro ou limpeza incompleta impedem
  a publicação, mesmo quando o relatório agregado diz `success`.
- Workflow, repositório, branch e estado ativo da lease precisam corresponder
  à ordem de trabalho. Overrides de destino divergentes são recusados.
- Este caminho usa o cliente de `github.com`; outros hosts são recusados.
- O endpoint manual de criação de PR não aceita ordens de fábrica. A publicação
  deve passar pelo grafo, sem contornar as evidências.

O serviço existente envia os artefatos acumulados das tarefas aprovadas por um
único commit via Git Data API. A árvore é montada sobre a base fixada. Em uma
nova publicação conhecida, o pai é o commit anterior da branch e a árvore
continua sendo reconstruída a partir da base e dos artefatos aprovados.

## Branch existente e retomada

Uma branch existente só pode ser atualizada quando seu SHA atual coincide com
o commit anterior registrado no checkpoint. Não há force-push. Colisão de nome,
avanço externo ou branch desconhecida produzem `factory_head_mismatch` antes
de qualquer escrita remota.

Se a consulta do CI falhar depois de criar o PR, sua URL, número, branch e
commit são preservados para a próxima tentativa. Um commit criado antes de
perder a resposta pode ser recuperado somente quando sua mensagem contém a
mesma intenção determinística, seus pais coincidem com o checkpoint/base e sua
árvore coincide com os artefatos esperados. Um retry reutiliza o PR exato;
um PR já fechado exige intervenção, sem criar outro automaticamente.

Anotações de CI identificam os arquivos e as últimas tarefas responsáveis.
Somente essas tarefas são reabertas. Falhas sem atribuição objetiva ou que
apontam para arquivos não publicados vão para decisão humana.

## Encerramento e ação humana

Somente um PR identificado com CI `success` chega ao estado terminal
`ready_for_human_review`. API, memória e dashboard preservam esse estado e
indicam que a próxima ação é revisar o PR e decidir o merge no GitHub.
O Forgehand não chama a API de merge.

CI `pending`, `none`, `skipped` ou erro de publicação/consulta levam a um gate
com `retry` e `abort`. Uma nova tentativa com tarefas já aprovadas repete a
publicação/consulta sem reexecutar o agente. Aceitação parcial não permite
contornar o gate da fábrica. Sem checks, o operador precisa configurar CI ou
encerrar o workflow; o sistema não inventa uma validação verde.

Workflows legados preservam seu comportamento e o estado `completed`.
Factory mode segue desabilitado por padrão. Testes locais cobrem recuperação
SCM simulada, retomada do grafo e execução real em Docker. O benchmark pago de
PRs ainda precisa ser executado; veja `factory-qualification.md` e
`factory-lifecycle.md` para evidências, operação e limitações.
