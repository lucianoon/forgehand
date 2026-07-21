# Runbook de produção

## Pré-implantação

1. Copie `.env.example` para `.env` e substitua todas as credenciais.
2. Use `API_KEYS_JSON` sem a chave default e separe roles por pessoa/serviço.
3. Configure backup do volume `pgdata` e retenção do arquivo de auditoria.
4. Restrinja a porta do PostgreSQL à rede interna no ambiente final.
5. Mantenha o executor de arquivos desabilitado até validar o sandbox Docker.

## Subida e verificação

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
curl --fail http://localhost:8000/health
curl --fail http://localhost:8000/readyz
curl --fail http://localhost:8000/metrics/prometheus
```

`APP_PORT` controla somente a porta publicada no host. O serviço continua
ouvindo em 8000 dentro da rede do Compose. O worker não possui healthcheck HTTP;
sua disponibilidade aparece em `/readyz` e nas métricas de workers da API.
O serviço one-shot `audit-init` ajusta a propriedade do volume persistente e
encerra antes da API; a API continua executando como usuário não-root.

O deploy só está saudável quando `/readyz` responde 200, a fila responde e a
quantidade esperada de workers está ativa.

## Alertas mínimos

- `agent_forge_queue_failed > 0` por cinco minutos;
- fila crescendo sem workers ocupados;
- `/readyz` diferente de 200;
- p95 HTTP acima do SLO acordado;
- benchmark periódico abaixo do quality gate;
- custo médio por workflow acima do limite comercial.

## Incidente e rollback

1. Bloqueie novas criações removendo as API keys de operadores.
2. Cancele workflows ativos pela API antes de interromper workers.
3. Preserve PostgreSQL e auditoria para investigação.
4. Reverta a imagem da API e do worker para a tag anterior.
5. Execute smoke test, benchmark e valide `/readyz` antes de reabrir tráfego.

Nunca apague checkpoints para resolver uma fila presa. Inspecione locks,
expiração de lease e auditoria primeiro.

## Teste PostgreSQL isolado

O target `test` da imagem contém os extras de desenvolvimento sem adicioná-los
à imagem final:

```bash
docker build --target test -t agent-forge:test .
docker run --rm \
  -e RUN_POSTGRES_TESTS=1 \
  -e TEST_DATABASE_URL=postgresql://forge:forge@host.docker.internal:5432/agent_forge_test \
  agent-forge:test pytest tests/integration/test_postgres_restart.py -q
```

Use um banco separado: workers de produção não devem consumir jobs da suíte.
