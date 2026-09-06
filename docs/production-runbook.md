# Operação do piloto de produção

Este é o runbook da instalação para **uma equipe, em um host Linux dedicado com
Docker Engine local**, mantendo revisão e merge humanos. O perfil canônico é
`docker-compose.team.yml`, independente. Não combine com `docker-compose.yml` ou
`docker-compose.prod.yml`: esses arquivos anteriores não representam a instalação
de equipe, seus paths de sandbox, sua auditoria ou seu procedimento de backup.

A existência deste documento não significa que um ambiente de produção foi
instalado ou qualificado. Registre o aceite de cada instalação em
[pilot-readiness.md](pilot-readiness.md). Preparação do host e configuração estão em
[team-installation.md](team-installation.md); o contrato de recuperação está em
[team-backup.md](team-backup.md).

## Responsáveis e limites

Antes de abrir acesso, registre um operador e substituto, responsável por credenciais
e orçamento, e revisores dos repositórios. O operador responde por atualizações,
capacidade, backups fora do host, restauração, TLS e atendimento a alertas. Cada
revisor responde pelo aceite da mudança e seu merge. Não há plantão, coleta de
métricas, proxy TLS ou agenda de backup criados automaticamente pelo Compose.

O host não deve compartilhar o daemon Docker com cargas não confiáveis. O socket
permite administrar o host; UID não root e sandbox não transformam essa instalação
em isolamento entre empresas. Restrinja GitHub App/token aos repositórios do piloto.
Mantenha banco, socket, métricas e diagnóstico na rede de administração. A API
publica em `127.0.0.1` por padrão: configure e teste um proxy TLS antes do acesso
remoto. Não exponha PostgreSQL nem o socket pela Internet.

## Registro de uma versão

Mantenha, fora dos dados da aplicação, um registro sem segredos com:

- SHA completo revisado, resultado e URL do CI, imagem da aplicação por digest,
  arquitetura, digest PostgreSQL 16 e digests das imagens dos perfis de build;
- revisão do Compose e identificador do projeto, caminho absoluto dos dados,
  quantidade esperada de workers, fingerprint e resultado de `forgehand doctor`;
- referência privada da configuração e credenciais recuperáveis separadamente;
- versão anterior, sua imagem/configuração, backup anterior à mudança e decisão
  explícita sobre compatibilidade de banco, checkpoints e journal.

Uma tag exclusiva serve para construir; o artefato promovido deve ser identificado
por digest de registro. Para um ensaio sem registro, conserve o image ID e uma cópia
exportada com hash, sem afirmar que isso já é distribuição por registro. O label
de revisão identifica a origem declarada; não substitui CI nem validação do build.
API e todos os workers devem receber a mesma imagem e configuração.

Para produção, construa o target `runtime` pelo Dockerfile e lockfile revisados,
publique pelo processo de release adotado pela equipe e qualifique esse artefato.
Uma imagem local obtida copiando fontes sobre outra imagem é evidência de ensaio;
não substitui a qualificação do artefato de release.

## Implantação inicial e atualização coordenada

Os exemplos assumem o checkout da versão escolhida, projeto Compose `forgehand-team`,
porta local 8000 e dois workers. Use **o mesmo nome de projeto em toda operação**:
mudar `-p` pode selecionar outro volume PostgreSQL. Ajuste porta e contagem juntos
com a configuração privada. Nunca execute o arquivo de configuração com `source`.

```bash
export TEAM_ENV_FILE=/etc/forgehand/team.env
export TEAM_PROJECT=forgehand-team
docker compose --env-file "$TEAM_ENV_FILE" -p "$TEAM_PROJECT" -f docker-compose.team.yml config --quiet
```

Para uma atualização:

1. Prepare a imagem nova, seus perfis e a configuração privada nova, preservando
   a configuração anterior. Em imagem de registro, baixe explicitamente o digest
   revisado antes de parar serviços; `pull_policy: never` impede download implícito.
   Confira somente ID/label, sem imprimir `Config.Env` ou o Compose resolvido.
2. Bloqueie novas submissões no proxy e nos clientes automatizados, mantendo acesso
   administrativo para concluir ou cancelar trabalho. O produto não tem endpoint
   de manutenção global. Apenas remover uma chave ou parar a API não drena workers.
3. Reconcilie **todos** os clientes/projetos com o registro de admissões do piloto.
   Aguarde jobs enfileirados/em processamento e decida ou cancele gates pendentes.
   `GET /workflows` é limitado ao proprietário e a 100 resultados: uma consulta de
   administrador não prova inventário global. Fila zerada também não prova ausência
   de aprovação pendente. Registre cada execução resolvida, incluindo fluxos legados.
4. Pare API/workers, deixando PostgreSQL ligado. Faça o backup frio verificável da
   versão anterior conforme abaixo; mantenha a origem em manutenção até terminar.
5. Confirme a análise de schema/checkpoints e o caminho de rollback. Não inicie
   binário novo sobre dados antigos sem esse registro: startup pode criar/alterar
   schema e o fingerprint não é uma garantia de compatibilidade de downgrade.
6. Atualize `FORGEHAND_IMAGE` e `FORGEHAND_REVISION` no arquivo privado para o
   artefato revisado. Recrie API e workers juntos; não misture versões em rolling
   update. Não altere a versão principal do PostgreSQL nessa mesma operação.

Na primeira instalação, os dados são novos; prepare-os segundo a instalação e
registre o backup inicial após o aceite. Na atualização, execute o backup antes de
alterar o arquivo privado:

```bash
docker compose --env-file "$TEAM_ENV_FILE" -p "$TEAM_PROJECT" -f docker-compose.team.yml stop api worker
# /srv/forgehand-backups deve existir com proprietário do runtime e modo 0700.
# O nome de cada bundle deve ser novo; use o caminho absoluto real da instalação.
docker compose --env-file "$TEAM_ENV_FILE" -p "$TEAM_PROJECT" -f docker-compose.team.yml run --rm --no-deps \
  --volume /srv/forgehand-backups:/backups worker \
  python -m app.operations.team_backup backup \
  --data-root /srv/forgehand-data --output /backups/pre-release-UNIQUE_ID \
  --database-url-env DATABASE_URL
```

Esse comando substitui o worker pelo processo de manutenção. Registre o manifest,
hashes e cópia protegida fora do host. Não copie configuração com segredos para o
data root; ela não deve entrar no bundle. Após selecionar a nova versão no arquivo:

```bash
docker compose --env-file "$TEAM_ENV_FILE" -p "$TEAM_PROJECT" -f docker-compose.team.yml config --quiet
docker compose --env-file "$TEAM_ENV_FILE" -p "$TEAM_PROJECT" -f docker-compose.team.yml up -d --no-build --force-recreate --scale worker=2 api worker
curl --fail --silent --show-error http://127.0.0.1:8000/health
curl --fail --silent --show-error http://127.0.0.1:8000/readyz
forgehand --url http://127.0.0.1:8000 doctor --json
```

`FORGEHAND_API_KEY` no ambiente da CLI deve ser uma chave administrativa; não a
passe em argumento. Aceite exige readiness, dois workers **compatíveis**, revisão
e fingerprint esperados e nenhum job incompatível, `legacy_unbound` ou sem
configuração. Diagnóstico não chama LLM/GitHub nem prova credenciais remotas.
Execute uma entrega canário autorizada, confira PR, SHA, CI e verificação
independente, então reabra o acesso. Um `healthy` do Docker sozinho não libera uso.

## Observação e resposta

Integre sondas e métricas ao monitoramento do operador e teste o recebimento de um
alerta antes do piloto. Os valores abaixo são **pontos de partida para o piloto**,
a ajustar ao volume e timeout dos perfis; não são SLOs já medidos. Use coleta a cada
30 segundos e uma janela de manutenção explícita para os procedimentos acima.

| Sinal | Condição inicial | Ação do operador |
| --- | --- | --- |
| Sonda HTTP `/readyz` | Resposta diferente de 200 ou timeout por 2 minutos | Bloquear novas admissões; verificar banco, heartbeats e `doctor` |
| `forgehand_workers_registered` | Menos que `INSTALLATION_EXPECTED_WORKERS` por 2 minutos | Verificar containers, versão e conexão com PostgreSQL; readiness confirma compatibilidade |
| `forgehand_queue_queued` e `forgehand_queue_processing` | Fila maior que zero sem processamento por 5 minutos | Inspecionar leases, compatibilidade e logs; não apagar checkpoints |
| `forgehand_queue_failed` | Maior que zero por 5 minutos | Investigar os jobs e classificar falha; acompanhar até reconciliação |
| Sonda da coleta | Falha de scrape por 2 minutos | Verificar API/rede; ausência de série não equivale a zero falhas |
| Disco e backup do host | Espaço livre abaixo de 20% ou backup mais antigo que o RPO acordado | Pausar admissão antes de esgotar espaço; verificar retenção e cópia externa |

As métricas com prefixo `forgehand_` acima existem em `/metrics/prometheus`. São
gauges de estado; o número de falhas pode continuar positivo após o incidente.
Use acompanhamento do incidente, sem apagar registros para silenciar o alerta.
Identifique a instalação na configuração do coletor. Readiness e coleta são endpoints
sem autenticação na aplicação, por isso devem permanecer privados no proxy/firewall.

No perfil com workers externos, `forgehand_workers_running`,
`forgehand_workers_busy` e `forgehand_workflows_active` descrevem o processo da API
e podem ser zero durante entregas. Para esse perfil observe fila PostgreSQL,
workers registrados e readiness. Os contadores HTTP disponíveis são
`forgehand_http_requests_total` (labels `method`, `route`, `status`) e
`forgehand_http_request_duration_seconds_total` (`method`, `route`). Permitem
calcular taxa/latência média; **não há histograma para calcular p95 HTTP**.
Meça percentis no proxy ou em sondas. Custos e aceitação dos PRs vêm dos relatórios
de cada workflow e da revisão humana, não de uma métrica Prometheus de custo.

## Incidente, rollback e restauração

Primeiro bloqueie admissões, preserve logs/auditoria, IDs de workflows, imagem,
fingerprint e informação do incidente. Se for possível manter o sistema em execução
com segurança, conclua/cancele trabalho e confirme limpeza dos sandboxes antes do
backup. Uma falha abrupta exige reconciliar leases/journal; não remova locks,
marcadores de restauração ou checkpoints para forçar startup.

Há dois caminhos distintos:

1. **Reverter somente aplicação:** permitido apenas com compatibilidade de schema,
   checkpoints, journal, configuração e jobs documentada para a versão anterior.
   Preserve backup do estado atual, selecione a imagem/configuração anterior,
   recrie API e workers juntos e repita readiness, `doctor` e canário. Não basta
   trocar uma tag. Se houver jobs da versão nova incompatíveis, mantenha manutenção
   até reconciliá-los; não edite fingerprints para burlá-los.
2. **Restaurar o ponto anterior:** quando compatibilidade é desconhecida ou não
   existe, restaure o backup pré-mudança em PostgreSQL 16 **novo, vazio e com nome
   diferente**, mantendo a origem offline. Siga [team-backup.md](team-backup.md):
   para retomar, o data root deve ocupar o mesmo caminho absoluto original em host
   isolado, inicialmente inexistente, usando `--original-path`. Use a imagem,
   configuração e perfis do backup e a conexão do banco restaurado. A inspeção em
   outro caminho é offline; o marcador proíbe iniciar workers nessa cópia.

No segundo caminho, reconcilie todas as admissões e efeitos posteriores ao backup
antes de reabrir o piloto. PRs, branches e chamadas cobradas não são desfeitos pela
restauração; registros de idempotência criados após o backup também podem faltar.
Não reenvie pedidos automaticamente nem execute as duas instalações ao mesmo tempo.
Registre perda de dados/retrabalho e confira cada PR pelo SHA remoto. Nunca use
`docker compose down --volumes` para rollback ou manutenção.

## Medir recuperação e capacidade

O operador agenda backups frios com janela de indisponibilidade, cópia fora do host
e retenção compatível com o piloto. O comando não agenda, criptografa nem replica o
backup. Se o negócio exigir backup contínuo sem parada, este procedimento offline
precisa ser substituído por uma solução consistente de banco **e** arquivos antes
de prometer esse requisito.

Registre no ensaio: falha simulada em UTC (`T0`), ponto consistente do último backup
(`Tb`), prontidão restaurada (`T1`) e canário aprovado (`T2`). Calcule
`RPO observado = T0 - Tb` e `RTO observado = T2 - T0`, incluindo provisionamento,
restauração, reconciliação e verificação. Guarde também tempo do dump, tamanho dos
dados, hash do bundle e tempo até readiness. A duração isolada de `pg_restore` não
é RTO. Confirme quais submissões/checkpoints existiam em `Tb` e quais faltariam;
um backup com hash correto sozinho não prova recuperação.

Como meta inicial a aprovar, ensaie **RPO de 24 horas e RTO de 60 minutos**. São
limites propostos, não resultados nem compromisso de disponibilidade. Se forem
inaceitáveis à equipe, ajuste arquitetura/agenda e reensaie antes da liberação.
Um único host permanece um ponto único de falha, sem promessa de alta disponibilidade.

Execute o ensaio de instalação, reinício e restauração documentado em
[team-installation.md](team-installation.md) e a campanha em
[pilot-readiness.md](pilot-readiness.md). Use banco, projeto Compose e repositórios
de teste próprios; workers do piloto nunca devem consumir jobs da suíte de testes.
