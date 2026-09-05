# Integrações operacionais

## GitHub: entrega até o PR verde

### Credenciais

Duas formas, lidas do ambiente do processo (nunca por `Settings`, nunca
devolvidas pela API):

- **GitHub App** (recomendado, escopo por instalação e token curto):
  `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID` e a chave privada em
  `GITHUB_APP_PRIVATE_KEY` (com `\n` literais) ou `GITHUB_APP_PRIVATE_KEY_PATH`.
  Exige o extra `github-app` (`uv sync --extra github-app`). A App precisa de
  *Contents: read/write*, *Pull requests: read/write* e *Checks: read* no
  repositório.
- **Token estático**: `GITHUB_TOKEN`, com permissão de conteúdo, refs, pull
  requests e leitura de checks.

Se ambos existirem, a App tem prioridade. O modo fábrica também usa essas
credenciais no [checkout de repositórios privados](private-repositories.md).

### Entrega como etapa do workflow

Informe `delivery` ao criar o workflow (papel `approver`):

```http
POST /workflows
X-API-Key: ...
Content-Type: application/json

{
  "project_id": "svc",
  "request": "Corrija o endpoint de clientes...",
  "delivery": {"repository": "owner/name", "base_branch": "main",
               "wait_for_checks": true, "checks_timeout_seconds": 900}
}
```

Ao final, após `synthesize`, o nó `publish_delivery`:

1. reúne os arquivos finais (`workspace.published_files` / `deleted_paths`)
   das tarefas aprovadas;
2. publica **um único commit** na branch da entrega (Git Data API: árvore →
   commit → ref) e abre ou reutiliza o pull request. Retry com conteúdo
   idêntico não cria commit;
3. com `wait_for_checks`, acompanha check runs e statuses do commit até todos
   concluírem. Sem nenhum check após `DELIVERY_CHECKS_GRACE_SECONDS` o
   resultado é `none` (repositório sem CI); estourado o timeout, `pending`;
4. **CI vermelho é sinal objetivo**: as tarefas que publicaram arquivos são
   reabertas com as falhas (título, resumo e anotações `path:linha`) como
   `required_changes`, e o workflow volta ao replan. O ciclo é limitado por
   `max_iterations`; esgotado, cai no gate humano (`reason:
   ci_failed_iterations_exhausted`). `accept_partial` publica o que há e
   encerra sem voltar ao ciclo.

O status do workflow expõe `delivery` (PR, commit, `ci_state`, checks,
falhas, tentativas) e o `final_output` ganha uma seção "Entrega".
`DELIVERY_CHECKS_POLL_INTERVAL_SECONDS` controla a cadência do polling.

### Publicação manual

Para workflows criados sem `delivery`, a rota continua disponível:

```http
POST /workflows/{workflow_id}/pull-request
X-API-Key: ...
Content-Type: application/json

{"repository":"owner/name","base_branch":"main","wait_for_checks":true}
```

A resposta inclui `commit_sha`, `changed` (false quando a árvore já era
idêntica) e, com `wait_for_checks`, o objeto `ci` com estado, checks e falhas.

## Sandbox

Para executar validadores em container:

```bash
EXECUTOR_COMMAND_BACKEND=docker
EXECUTOR_SANDBOX_IMAGE=forgehand-sandbox:latest
EXECUTOR_SANDBOX_NETWORK_ENABLED=false
```

O runner aplica memória, CPU, limite de processos, `no-new-privileges`, remove
capabilities Linux e desabilita rede por padrão. O workspace é o único volume
gravável montado.

## Webhooks assinados

Configure `WEBHOOK_URLS_JSON` e `WEBHOOK_SIGNING_SECRET`. Eventos carregam
`X-Forgehand-Event` e `X-Forgehand-Signature-256`. O receptor deve calcular
HMAC-SHA256 sobre o corpo bruto e comparar em tempo constante.

## Benchmark

Com a API em execução:

```bash
export FORGEHAND_API_KEY=...
python -m app.evaluation.benchmark \
  --cases benchmarks/cases.json \
  --concurrency 2 \
  --output reports/benchmark.json \
  --fail-on-gate
```

O relatório inclui conclusão, first-pass, tokens, custo, duração média, p95 e
um quality gate configurável. Workflows que excedem o timeout são cancelados,
evitando consumo órfão depois do benchmark.

Sem expor uma porta HTTP, use `--in-process`; o fluxo ainda atravessa a API,
fila, worker, grafo e provider configurado no ambiente.

### Benchmark via GitHub Actions

O workflow manual `.github/workflows/benchmark.yml` roda o mesmo comando em
modo `--in-process` e publica os JSONs de `reports/` como artifact
(`benchmark-reports`, retenção de 90 dias). Ele nunca dispara em push — o
benchmark chama LLMs reais e custa dinheiro. Antes do primeiro uso, cadastre o
secret `OPENROUTER_API_KEY` no repositório (Settings > Secrets and variables >
Actions). Para disparar:

```bash
gh workflow run benchmark.yml
# opcional: subconjunto de casos e paralelismo
gh workflow run benchmark.yml -f case_ids=architecture-review,security-review -f concurrency=1
```

Baixe o resultado com `gh run download --name benchmark-reports` após o fim da
execução.

## Observabilidade

`GET /metrics/prometheus` expõe requests por rota/status, tempo HTTP acumulado,
workers, fila e workflows ativos no formato Prometheus. Use `/readyz` para
readiness e `/metrics` quando precisar do snapshot operacional em JSON.

## RBAC

Cada entrada de `API_KEYS_JSON` aceita `role`: `viewer`, `operator`, `approver`
ou `admin`. Operadores criam workflows; aprovadores também decidem e publicam
PRs; somente administradores consultam auditoria global.
