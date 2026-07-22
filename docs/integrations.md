# Integrações operacionais

## GitHub e pull requests

Defina `GITHUB_TOKEN` no ambiente do processo. O token precisa somente das
permissões do repositório necessárias para conteúdo, refs e pull requests.
Depois de um workflow produzir arquivos, use:

```http
POST /workflows/{workflow_id}/pull-request
X-API-Key: ...
Content-Type: application/json

{"repository":"owner/name","base_branch":"main"}
```

O token nunca passa por `Settings` nem é devolvido pela API.
Repetir a publicação é idempotente: branch, conteúdo inalterado e PR aberto são
reutilizados, permitindo retomar com segurança depois de uma falha parcial.

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

## Observabilidade

`GET /metrics/prometheus` expõe requests por rota/status, tempo HTTP acumulado,
workers, fila e workflows ativos no formato Prometheus. Use `/readyz` para
readiness e `/metrics` quando precisar do snapshot operacional em JSON.

## RBAC

Cada entrada de `API_KEYS_JSON` aceita `role`: `viewer`, `operator`, `approver`
ou `admin`. Operadores criam workflows; aprovadores também decidem e publicam
PRs; somente administradores consultam auditoria global.
