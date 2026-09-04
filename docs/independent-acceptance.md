# Aceitação independente de comportamento

Um build verde não confirma, sozinho, que o software atende ao cliente: o agente
que escreve o código também pode alterar seus testes. O Forgehand permite definir
casos de aceitação **fora do repositório candidato**, nos perfis do operador.

Neste marco, cada caso executa um programa de linha de comando e compara sua saída
com o resultado aprovado. O comparador e as expectativas ficam no host; não são
arquivos que o agente possa apagar ou reescrever. Imprimir "testes passaram" não
substitui uma saída de negócio correta.

## Contrato e cobertura

- Cada caso tem ID único, texto exato de um critério da ordem, comando e
  `expected_stdout`. Vários casos podem cobrir o mesmo critério.
- Uma suíte contém 1–8 casos de até 30 segundos cada, somando no máximo 120 segundos
  de execução configurada. Controle e cleanup Docker têm timeouts separados.
  Expectativa: até 8 KiB; captura por stream: até 16 KiB; suíte: até 64 KB.
- A seleção, antes do planejamento, fixa hashes da suíte/casos e critérios da
  ordem. Critério sem caso torna a estratégia `unsupported`. Isso inclui regras
  de preservação acrescentadas pelas entregas incrementais.
- A cobertura é declarada, com correspondência textual: o operador precisa julgar
  a suficiência dos exemplos. Associar um teste fraco a uma frase abrangente não
  prova essa frase. O modelo não inventa o resultado esperado.
- Alterar comandos, expectativas ou casos invalida a seleção persistida.

## Execução e evidência

Após as fases comuns e regras de arquitetura, os casos executam em containers
separados: imagem por digest, sem rede, workspace somente leitura e limites de
recursos existentes. `/tmp` é descartável e não é compartilhado entre casos.

O host compara SHA-256 da saída textual UTF-8 capturada **antes** da sanitização
dos logs. Espaços e quebra de linha final importam. O caso exige exit zero,
captura completa, saída exata, montagem somente leitura e cleanup confirmado.
Timeout, truncamento, falha de infraestrutura ou limpeza bloqueiam o resultado.

O relatório preserva caso, critério, hashes e execução. Grafo e publicação verificam
novamente cobertura e identidade de todos os casos; aprovação da IA ou CI verde
não substituem a evidência. Falhas entram no feedback de correção. O resumo informa
casos/critério/sucesso; perfis sem suíte são explicitamente **sem evidência de
aceitação independente**. Merge humano continua obrigatório.

## Exemplo

Valor de `FACTORY_BUILD_PROFILES_JSON`, mantido no ambiente administrado do servidor,
nunca no repositório que o agente edita:

```json
{
  "cli-integers": {
    "ecosystem": "python",
    "image": "python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea",
    "phases": [{
      "name": "test",
      "argv": ["/usr/local/bin/python", "-m", "unittest", "discover", "-s", "tests"]
    }],
    "acceptance": {
      "version": 1,
      "cases": [{
        "id": "positive-sum",
        "criterion": "Somar inteiros corretamente",
        "command": {
          "name": "test",
          "argv": ["/usr/local/bin/python", "calc.py", "2", "3"],
          "timeout_seconds": 10
        },
        "expected_stdout": "5\n"
      }, {
        "id": "negative-input",
        "criterion": "Somar inteiros corretamente",
        "command": {
          "name": "test",
          "argv": ["/usr/local/bin/python", "calc.py", "-2", "3"],
          "timeout_seconds": 10
        },
        "expected_stdout": "1\n"
      }]
    }
  }
}
```

O digest é o da imagem Python usada nas fixtures locais; revise/aprove a imagem
para sua instalação e disponibilize-a no daemon. Não há download automático.
A ordem deve escolher `build_profile: "cli-integers"` e
`acceptance_criteria: ["Somar inteiros corretamente"]`. O alvo precisa implementar
`calc.py` e seus testes comuns; os casos verificam a saída dessa implementação.
Para Node, use imagem aprovada e `/usr/local/bin/node` com a CLI da aplicação.

## Ativação e compatibilidade

Configure a suíte no perfil aprovado e atualize/reinicie API e workers de forma
coordenada, com a mesma configuração. Comandos seguem a política existente:
executável absoluto aprovado, argumentos/cwd dentro do workspace, sem shell,
credenciais ou rede. Neste marco o nome da fase dos casos deve ser `test`.

Perfis antigos preservam fingerprint e funcionamento. Aceitação é obrigatória
**para um perfil que a configure**, não globalmente para todos os perfis. Restrinja
os perfis oferecidos para sua operação: um perfil legado não produz essa garantia.
Workflows antigos não recebem casos retroativamente nem adotam política divergente
sem intervenção. Nenhum serviço é reiniciado por esta mudança de código.

## Limites

- Exemplos finitos podem ser decorados; não provam correção universal.
- Não há HTTP/browser, stdin interativo ou estado compartilhado entre casos.
  Programas que precisam escrever no checkout falham; podem usar `/tmp` local.
- O gate roda após cada tentativa de tarefa; planos grandes podem precisar de
  retries até o comportamento completo existir. Prefira entregas pequenas.
- Expectativas não são montadas nem enviadas no prompt inicial. Isso não garante
  segredo: hashes de valores simples podem ser adivinhados. Nunca use segredos
  nos casos; critérios e saídas observadas aparecem na evidência.
- Host, runner e daemon Docker são confiáveis nessa fronteira; não há atestação
  contra operadores ou infraestrutura comprometidos.
- Não certifica segurança, escalabilidade, migrações, desempenho ou todas as regras
  de negócio. Esses aspectos precisam de avaliações próprias e revisão humana.

## Verificação

`tests/unit/test_independent_acceptance.py` cobre limites, cobertura, drift,
falsos verdes, manipulação/truncamento, cleanup/cancelamento, persistência e veto
no grafo/publicação. `tests/integration/test_acceptance_docker.py` verifica programa
errado, corrigido e escrita proibida em Python/Node reais. Requer imagens já
instaladas, `RUN_FACTORY_DOCKER_TESTS=1` e `FACTORY_DOCKER_SOCKET`.

Esses testes verificam o mecanismo do Forgehand, não a entrega de um produto de
cliente específico. Não chamam IA paga, criam PRs nem fazem deploy.

Validação local em 2026-09-03: **728 testes Python passaram, 35 opcionais pulados**,
com o modo de integração Docker habilitado e casos reais nas imagens Python/Node
fixadas. Passaram também 14 testes JavaScript, Ruff, mypy (78 módulos) e OpenSpec
estrito. O exemplo JSON acima é validado por teste. Não é um teste de carga ou
uma qualificação de produção; ativação requer configurar os casos do produto.
