# Entregar mudanças por PR pelo terminal

`forgehand deliver` envia uma ordem de trabalho à fábrica existente. O mesmo
fluxo pode ser iniciado no dashboard. O comando acompanha a execução até o PR
ficar pronto para revisão, preservando a espera pelo CI e o merge humano.

## Preparar o servidor

A API precisa de factory mode habilitado, credencial GitHub, provedor LLM e
perfil de build aprovado. Veja [entrega da fábrica](factory-delivery.md) e
[perfis de build](factory-build-profiles.md). O token usado pela CLI é uma
**API key do Forgehand**, com papel `approver` ou `admin` e acesso ao projeto.
As credenciais OpenAI/GitHub ficam no servidor.

`FORGEHAND_URL` e `FORGEHAND_API_KEY` configuram a CLI. As opções globais
`--url`, `--api-key` e `--json`, quando necessárias, vêm antes do subcomando.
Evite colocar chaves em argumentos de scripts compartilhados ou no histórico.

## Conferir a ordem antes de enviar

```bash
uv run forgehand deliver --project equipe \
  --repository organizacao/repositorio --base-ref main \
  --request "Corrija o cálculo de desconto sem alterar a API pública." \
  --criterion "Desconto zero, parcial e integral preservam o total esperado" \
  --criterion "Um teste de regressão detecta a aplicação duplicada do desconto" \
  --budget-usd 0.50 --build-profile python-projeto \
  --idempotency-key equipe-desconto-42 --dry-run
```

O `--dry-run` valida os argumentos e imprime o JSON completo de criação. Não
consulta a API nem inicia IA ou GitHub; portanto não certifica permissões,
acesso ao repositório, existência da branch ou disponibilidade do build.
O perfil `python-projeto` é ilustrativo: use um nome aprovado no seu servidor.

Retire `--dry-run` para enviar exatamente essa solicitação. O orçamento é
obrigatório e vale por workflow. Custos são estimativas medidas pelo aplicativo;
chamadas em andamento podem ultrapassar o limite de acompanhamento.

## Partir de uma issue

```bash
uv run forgehand deliver --project equipe \
  --issue https://github.com/organizacao/repositorio/issues/42 \
  --criterion "A regressão descrita na issue passa" \
  --budget-usd 0.50 --build-profile python-projeto \
  --idempotency-key equipe-issue-42
```

A API captura o título e o corpo da issue. `--issue` substitui a combinação
`--repository` e `--request`; critérios de aceite continuam obrigatórios.
`--expected-base-sha` fixa um commit conhecido somente na origem direta;
nessa modalidade a fábrica rejeita uma base que tenha mudado.

## Acompanhar e retomar

```bash
uv run forgehand deliver --project equipe \
  --repository organizacao/repositorio \
  --request "Adicione testes de regressão para descontos." \
  --criterion "Os testes detectam o desconto aplicado duas vezes" \
  --budget-usd 0.50 --idempotency-key equipe-testes-43 --no-wait

uv run forgehand wait ID_DO_WORKFLOW
uv run forgehand --json wait ID_DO_WORKFLOW
uv run forgehand status ID_DO_WORKFLOW
```

`--no-wait` imprime o ID e retorna após a admissão. `wait` somente consulta esse
workflow; não inicia outro trabalho. `--timeout` e `--poll-seconds` controlam a
espera da CLI. Encerrar o terminal ou atingir esse timeout **não cancela** o
workflow: use `wait` para continuar ou `cancel` para cancelamento explícito.
`--max-wall-clock-seconds`, `--max-tokens` e `--max-iterations` limitam o trabalho
no servidor, separadamente do tempo de espera do terminal.

O comando informa uma chave de idempotência antes do envio. Se a resposta de
criação se perder, preserve a mesma solicitação e a mesma chave ao reenviar;
não é feito reenvio automático. Se o ID já estiver disponível, prefira `wait`.
A deduplicação depende de preservar o registro da fila; use PostgreSQL
para que esse registro sobreviva a reinícios do servidor.

A entrega pronta imprime URL do PR, SHA, estado do CI e custo. O estado
`ready_for_human_review` encerra a espera com sucesso; significa PR para revisar,
não merge. Um gate humano, falha ou cancelamento permanece distinto de sucesso.
Use `decide` para responder a uma decisão pendente e `wait` para acompanhar.

| Código de saída | Significado |
| --- | --- |
| 0 | Entrega concluída/pronta para revisão, envio aceito sem espera ou consulta bem-sucedida |
| 1 | Workflow falhou, foi cancelado ou exige decisão humana |
| 2 | Argumentos, acesso, resposta HTTP ou transporte impediram a operação |
| 3 | Tempo de espera da CLI esgotado; execução remota não foi cancelada |

`--json` mantém o estado completo para scripts. O comando `run` continua usando
o fluxo genérico anterior, sem converter solicitações existentes em publicação.

## Repositórios privados

O checkout da fábrica aceita a credencial GitHub configurada no servidor.
Veja [checkout privado](private-repositories.md) para permissões, renovação,
retomada e limites de isolamento entre equipes. O token GitHub não é enviado
pela CLI nem incluído na ordem de trabalho.
