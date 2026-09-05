# Recuperação de workers e decisões

A confirmação da fila significa que a invocação terminou, inclusive quando
parou num gate humano. Ela não deve ocorrer se o worker foi cancelado durante
execução. Nesse caso, a entrega mantém o lease até expirar e pode ser obtida
por outro worker, dentro do máximo de tentativas já configurado.

## Retomada do checkpoint

Numa reentrega de `start`, o serviço consulta o checkpoint sob o lock de
workspace quando esse lock existe:

- sem checkpoint: envia o pedido inicial;
- com trabalho pendente e sem interrupt: continua com `ainvoke(None)`;
- com interrupt pendente: mantém a aprovação para o operador;
- sem trabalho pendente: confirma a fila sem executar o grafo novamente.

A decisão usa o trabalho pendente do checkpoint, não apenas a fase do workflow:
uma fase `completed` pode ainda ter um nó de persistência para executar.

## Identidade das aprovações

`decide()` grava uma mensagem versionada na fila, com a decisão, os IDs dos
interrupts e sua posição persistida naquele momento (checkpoint, task e número
de valores já consumidos pelo nó). O envelope atual usa `resume_version=2`. O worker aplica o valor somente aos IDs
correspondentes via `Command(resume={id: decision})`.

O ID do interrupt e o ID do checkpoint podem se repetir em chamadas sequenciais
no mesmo nó, inclusive com o mesmo texto. A posição usa o comprimento da lista
`__resume__` da task nos pending writes do checkpointer; os valores das decisões
não são copiados para essa identidade. A leitura usa o contrato do LangGraph
fixado no lockfile e tem regressão com memória e PostgreSQL.

Se a decisão já foi consumida, o worker continua com os valores já persistidos,
sem reenviar a decisão. Se existe outro gate, confirma o job antigo e preserva o
novo interrupt, que continua exigindo uma decisão explícita.

Mensagens legadas de texto continuam aceitas na primeira entrega. Na
reentrega, podem continuar um checkpoint sem interrupts ou confirmar um grafo
já finalizado. Se há um interrupt pendente, não há identidade suficiente para
provar que ele foi aprovado: o job falha com `resume_decision_unbound`, o
checkpoint permanece intacto e o operador pode enviar uma nova decisão.
Envelopes antigos com `resume_version=1`, contendo apenas IDs, continuam aceitos
na primeira entrega. Reentregas com um ID ainda pendente são ambíguas e exigem
nova decisão; sem IDs correspondentes, preservam outro gate ou continuam trabalho
normal. Mensagens versionadas inválidas também não alteram o grafo.

Se o checkpointer não fornece uma posição verificável, ou a task depende de um
checkpoint de subgrafo ainda não resolvido, a API recusa a decisão e preserva o
gate. Não assume posição zero em namespaces desconhecidos.

## Compatibilidade de implantação

Atualize API e workers juntos, interrompendo temporariamente novas decisões e
parando os workers antigos antes de ativar a API nova. Workers antigos não
entendem a mensagem de decisão versionada. Não faça rollback apenas dos
workers enquanto houver mensagens novas na fila; preserve banco e checkpoints
para uma recuperação compatível. Não apague jobs ou checkpoints para contornar
uma aprovação bloqueada.

## Verificação

Com um PostgreSQL de teste separado:

```bash
RUN_POSTGRES_TESTS=1 TEST_DATABASE_URL=postgresql://usuario:senha@localhost:5432/forgehand_test \
  uv run pytest tests/integration/test_worker_crash.py tests/unit/test_worker_shutdown.py tests/unit/test_resume_recovery.py tests/unit/test_resume_same_node.py -q
```

Os testes criam schemas únicos e os removem ao encerrar. Os filhos usam o
serviço, a fila e o checkpointer reais, com nós determinísticos sem LLM ou SCM.
O pai observa o checkpoint/evento da etapa e mata o processo com SIGKILL; um
segundo processo recupera a entrega após expiração do lease. Também são
verificados o número de tentativas e a preservação do gate seguinte, incluindo
duas aprovações no mesmo nó com payloads diferentes ou idênticos.

## Limites

Isto impede reiniciar etapas já confirmadas por checkpoint. Um efeito externo
feito dentro de um nó, antes de seu checkpoint, ainda pode ser repetido após
uma falha. Publicação SCM e outras integrações precisam manter sua própria
idempotência/reconciliação; não há garantia geral de efeitos exactly-once.

Os testes não simulam perda do host/banco, partição prolongada de rede ou
execução contra um provedor pago. A identidade do interrupt protege a
reentrega da fila; não adiciona controle de versão ao clique de uma interface
antiga nem resolve toda concorrência entre operadores.
