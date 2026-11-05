# Recuperação de edição com código atual

O piloto Node registrou tentativas com trechos ausentes, saída TAP usada como
fonte e `expect` inexistente num projeto baseado em `node:assert`. Duas lacunas
do controlador foram reproduzidas e corrigidas sem alterar os gates de entrega.

## Descoberta de código Node

O filtro compartilhado pelo grounding e por `search_repository` reconhece
`.js`, `.cjs`, `.mjs`, `.jsx`, `.ts`, `.cts`, `.mts` e `.tsx`. O fixture Node
passa a fornecer implementação, testes e os imports que identificam o framework.
Os limites de tamanho, relevância e diretórios ignorados continuam valendo.

## Releituras antes de corrigir

Quando existe resultado de uma tentativa anterior, ou uma rodada de autocorreção
vai começar, o executor seleciona os caminhos registrados pelo runtime:

1. alvos de operações que falharam;
2. arquivos das operações mais recentes;
3. demais arquivos aplicados, sem duplicatas.

O ToolLoop relê até quatro caminhos pelo `read_file` já configurado. Essas
chamadas contam no mesmo teto de exploração, passam pelos hooks pre/post/error
e aparecem no trace como `source=recovery_refresh`, na rodada 0. Não exigem
uma completion adicional só para pedir a leitura. Seu conteúdo entra no input
da completion seguinte e pode aumentar o consumo de tokens.

Não é criado outro leitor usando `workspace_root` de resultados/checkpoints.
Caminhos externos, arquivos sensíveis, bloqueios e supressões seguem a política
da ferramenta existente. Ferramentas desabilitadas ou `read_file` ausente não
ativam uma leitura alternativa; orçamento de tarefa já esgotado não ganha
novas releituras. Cada saída segue o limite configurado de caracteres.

O grounding inicial permanece como contexto histórico cacheável. As leituras
atuais chegam em mensagens de ferramenta separadas e prevalecem para edição.
O prompt distingue código, números de linha e diagnósticos stdout/stderr/TAP,
e orienta o uso do framework efetivamente observado.

## Evidência e limites

```bash
uv run pytest tests/unit/test_repository_grounding.py tests/unit/test_recovery_reads.py tests/integration/test_node_source_repair.py -q
```

A integração usa arquivos e execução Node reais, com respostas de modelo
determinísticas. Primeiro aplica uma edição inválida; a recuperação recebe
o código atualizado, aplica a correção e é verificada novamente por Node em
processo separado. Também há testes para replace sem correspondência, retry
externo, limites, leitura negada, saída suprimida e prioridade dos caminhos.

Isso comprova disponibilidade e controle das evidências de recuperação. Não
comprova que um LLM escolherá sempre o trecho correto ou aumenta retroativamente
o resultado 4/5 do piloto. Uma nova qualificação completa com LLM e verificadores
independentes ainda é necessária. Arquivos além dos limites ou sem caminhos no
resultado anterior continuam dependendo da exploração normal do agente.
