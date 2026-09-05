# Instalação para uma equipe

Este perfil sobe uma API, PostgreSQL 16 e dois processos de worker em **um host
Linux com Docker Engine local**. A equipe envia entregas pela CLI ou dashboard;
cada execução continua exigindo revisão e merge humano. O Studio fica desativado.

Use somente `docker-compose.team.yml`: ele é independente dos arquivos Compose
de desenvolvimento e produção antigos. A imagem contém Git, Docker CLI,
certificados TLS, PostgreSQL client 16, checkpointer PostgreSQL e autenticação por
GitHub App. API e workers usam a mesma imagem, revisão, perfis e arquivo de
configuração. As dependências Python são instaladas de `uv.lock`, com hashes.

## Preparar o host e a configuração

Requisitos: Linux/POSIX, Docker Engine local, Compose 2.24 ou mais recente, e
espaço para PostgreSQL, checkouts e imagens de build. A configuração do mount usa
`env_file.required` e `bind.create_host_path`, conforme a
[referência oficial do Compose](https://docs.docker.com/reference/compose-file/services/).
Este perfil não oferece múltiplos hosts nem compartilhamento por NFS.

O socket Docker permite criar containers e montar diretórios do host. Acesso ao
socket representa poder administrativo sobre esse host, mesmo com UID 1000 e
`no-new-privileges`. Use um host dedicado e credenciais restritas aos repositórios
da equipe. A API escuta somente em `127.0.0.1` por padrão; acesso remoto exige um
proxy TLS configurado pelo operador antes de mudar `APP_BIND_ADDRESS`.

1. Copie `deploy/team.env.example` para um arquivo privado **fora do checkout**.
   Exemplo: `/etc/forgehand/team.env`, permissão `0600` e acesso somente ao operador.
   Não coloque esse arquivo dentro de `FORGEHAND_DATA_ROOT`: ele contém segredos e
   não deve entrar no backup dos dados.
2. Substitua `FORGEHAND_REVISION` pelo SHA de 40 caracteres do código revisado e use
   uma tag exclusiva em `FORGEHAND_IMAGE`, por exemplo `forgehand:<sha>`. Para uma
   imagem distribuída por registro, prefira referência por digest e confira seu
   label `org.opencontainers.image.revision`. Não reutilize uma tag para outro build.
3. Defina uma senha PostgreSQL aleatória e a mesma senha em `DATABASE_URL` usando
   o hostname interno `postgres`, usuário/banco `forgehand`, porta 5432. Caracteres
   reservados da senha precisam de percent-encoding na URL. A porta do banco não
   é publicada no host.
4. Configure `API_KEYS_JSON` com chaves individuais e projetos/roles apropriados.
   `deliver` requer `approver` ou `admin`. Configure um backend LLM e sua chave;
   OpenAI, OpenRouter e Anthropic chegam igualmente à API e aos workers. Nenhuma
   chamada de modelo é feita pela subida da instalação.
5. Configure `GITHUB_TOKEN` restrito aos repositórios autorizados, ou os três campos
   `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY`. O App tem
   prioridade quando completo. A chave PEM pode usar `\n` literais, entre aspas
   simples no arquivo. Não coloque credenciais em URLs, perfis ou solicitações.
6. Defina `FACTORY_BUILD_PROFILES_JSON` e, quando necessário,
   `FACTORY_REPOSITORY_PROFILES_JSON` com perfis aprovados conforme
   [perfis de build](factory-build-profiles.md). `{}` permite inspecionar a
   instalação, mas não configura uma entrega funcional. Baixe no daemon as imagens
   exatas dos perfis antes de executar; os sandboxes usam `--pull never`.

Valores entre aspas simples no arquivo Compose preservam caracteres literais,
incluindo `$`. Não execute o arquivo com `source`; o Compose o interpreta. Não
publique a saída de `docker compose config` ou de `docker inspect`: elas podem
conter as credenciais resolvidas. A validação `config --quiet` não as imprime.

## Diretórios e acesso ao Docker

Use um único `FORGEHAND_DATA_ROOT` **absoluto**, sem links simbólicos, no host
Docker. Exemplo `/srv/forgehand-data`. Ele é montado no container com o **mesmo
caminho absoluto**. Isso é necessário porque os sandboxes irmãos são criados pelo
daemon do host e recebem caminhos de checkout produzidos pelo worker.

Prepare o diretório e seus filhos com o UID/GID configurado; os defaults
`FORGEHAND_UID=1000` e `FORGEHAND_GID=1000` correspondem à imagem. O Compose não
cria diretórios ausentes como root. Use IDs não zero. Se precisar de outro usuário
local, ajuste essas duas variáveis e os argumentos `-o`/`-g` abaixo juntos:

```bash
sudo install -d -m 0750 -o 1000 -g 1000 \
  /srv/forgehand-data \
  /srv/forgehand-data/factory \
  /srv/forgehand-data/executor \
  /srv/forgehand-data/audit
stat -c '%g' /var/run/docker.sock
```

Preencha `DOCKER_SOCKET_GID` com o grupo numérico que tem permissão de leitura e
escrita no socket. `DOCKER_SOCKET_PATH` aponta para o socket no host; dentro dos
containers ele fica em `/var/run/docker.sock`. Não torne o socket gravável por
todos. API e workers usam UID/GID `1000:1000` por padrão, com esse grupo adicional.
Esses IDs podem ser ajustados por `FORGEHAND_UID`/`FORGEHAND_GID`, mantendo usuário
não root. O root filesystem é somente leitura; `/tmp` é temporário e os dados
ficam no bind.

| Conteúdo | Local persistente |
|---|---|
| Fila, checkpoints, idempotência e heartbeats | Volume Compose `pgdata` |
| Cache Git, checkouts, journal e leases da fábrica | `$FORGEHAND_DATA_ROOT/factory` |
| Workspace do fluxo analítico legado | `$FORGEHAND_DATA_ROOT/executor` |
| Auditoria da API | `$FORGEHAND_DATA_ROOT/audit/api.jsonl` |
| Auditoria de cada processo worker | `$FORGEHAND_DATA_ROOT/audit/worker-<hostname>.jsonl` |
| Trava operacional de manutenção | `$FORGEHAND_DATA_ROOT/.maintenance.lock` |

Cada worker tem arquivo de auditoria próprio para evitar compactações concorrentes
no mesmo JSONL. O endpoint de auditoria da API consulta seu arquivo; os arquivos
dos workers permanecem disponíveis no diretório e no backup. Logs stdout/stderr
continuam disponíveis pelo Docker. A memória semântica em processo não é persistida
neste perfil; fila, checkpoints e histórico operacional são PostgreSQL.

## Subir e verificar

Execute no checkout correspondente à revisão informada:

```bash
export TEAM_ENV_FILE=/etc/forgehand/team.env
docker compose --env-file "$TEAM_ENV_FILE" -f docker-compose.team.yml config --quiet
docker compose --env-file "$TEAM_ENV_FILE" -f docker-compose.team.yml build api
docker compose --env-file "$TEAM_ENV_FILE" -f docker-compose.team.yml up -d --no-build --scale worker=2
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/readyz
```

Para o diagnóstico detalhado, forneça uma chave Forgehand de administrador no
ambiente `FORGEHAND_API_KEY` do terminal de operação e execute:

```bash
forgehand --url http://127.0.0.1:8000 doctor --json
```

O diagnóstico não inicia IA, publica PR ou muda configurações. Credenciais
presentes ainda precisam de uma entrega autorizada para comprovar acesso remoto.

O mesmo `FORGEHAND_IMAGE` é usado por API e workers; `pull_policy: never` impede uma
atualização implícita de imagem. Ao usar imagem por digest já publicada, carregue-a
antes e omita `build`. O build instala o client 16 a partir do
[repositório oficial PostgreSQL](https://www.postgresql.org/download/linux/debian/).
As bases e pacotes APT recebem patches nos próximos builds: guarde o digest da
imagem resultante junto da revisão usada no ensaio, não apenas a tag das bases.

`INSTALLATION_EXPECTED_WORKERS=2` corresponde aos dois processos; cada processo tem
`WORKFLOW_WORKER_CONCURRENCY=1`. Para alterar capacidade, altere a contagem esperada
e `--scale worker=N` juntos e recrie a API. O healthcheck HTTP da imagem comprova
que o processo responde; `/readyz` e o diagnóstico administrativo verificam a
coordenação necessária para aceitar trabalho. Um container `healthy` sozinho não
certifica credenciais remotas, qualidade de entregas ou capacidade de carga.

Com a instalação saudável, faça a primeira entrega autorizada usando
[`forgehand deliver`](developer-delivery-cli.md). Antes de usar repositórios reais,
execute o ensaio de instalação com provedor determinístico, reinício de worker e
restauração em destino isolado. A instalação só deve ser registrada como validada
quando essas verificações tiverem resultados, não apenas configuração aprovada.

## Manutenção, backup e recuperação

API e workers são iniciados pelo wrapper `app.operations.team_backup run`, que
mantém uma trava compartilhada enquanto o processo real vive. Pare ambos antes
de um backup offline; o módulo de backup recusa uma instalação ainda em uso.
PostgreSQL permanece disponível para o dump consistente:

```bash
docker compose --env-file "$TEAM_ENV_FILE" -f docker-compose.team.yml stop api worker
```

Use o módulo `app.operations.team_backup` ou `scripts/team-backup`, com
`DATABASE_URL` no ambiente, PostgreSQL client 16 e o data root completo. Por
exemplo, com o diretório externo de backups previamente criado para o mesmo
UID/GID da instalação:

```bash
docker compose --env-file "$TEAM_ENV_FILE" -f docker-compose.team.yml run --rm --no-deps \
  --volume /srv/forgehand-backups:/backups worker \
  python -m app.operations.team_backup backup \
  --data-root /srv/forgehand-data --output /backups/ensaio-001 \
  --database-url-env DATABASE_URL
```

O comando substitui a inicialização do worker: somente o backup roda. O destino
`ensaio-001` precisa ser novo. A imagem fornece `pg_dump`/`pg_restore` 16, e o
container administrativo alcança PostgreSQL pela rede interna. Não passe a senha
na linha de comando. O arquivo de credenciais externo não faz parte do backup.

A restauração exige um banco PostgreSQL 16 **diferente e vazio** e um diretório
de destino **inexistente**. Prepare somente seu diretório pai, com o UID/GID da
instalação. Use `docker run` com esse pai montado no mesmo caminho absoluto: o
mount do Compose exige que o data root já exista, e por isso `compose run worker`
não serve para esta operação. O backup deve ser montado somente para leitura.

Crie `/etc/forgehand/restore.env` com permissão `0600` e apenas
`RESTORE_DATABASE_URL=postgresql://.../forgehand_restore`, apontando para o banco
vazio previamente criado. Este arquivo usa o formato de `docker run --env-file`:
valor literal, **sem aspas**, sem interpolação e sem copiar as aspas do arquivo
Compose. A senha continua fora dos argumentos e o container não precisa das
credenciais LLM/GitHub nem do socket Docker.

Exemplo de restauração isolada para inspeção, usando a mesma imagem do backup e
um pai dedicado; ajuste `TEAM_IMAGE` e UID/GID aos valores da instalação:

```bash
TEAM_IMAGE=forgehand:sha-da-imagem-do-backup
POSTGRES_CONTAINER=$(docker compose --env-file "$TEAM_ENV_FILE" -f docker-compose.team.yml ps -q postgres)
TEAM_NETWORK=$(docker inspect --format '{{range $name, $config := .NetworkSettings.Networks}}{{$name}}{{end}}' "$POSTGRES_CONTAINER")
sudo install -d -m 0750 -o 1000 -g 1000 /srv/forgehand-recovery
docker run --rm --network "$TEAM_NETWORK" --user 1000:1000 \
  --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges \
  --env-file /etc/forgehand/restore.env \
  --mount type=bind,src=/srv/forgehand-recovery,dst=/srv/forgehand-recovery \
  --mount type=bind,src=/srv/forgehand-backups,dst=/backups,readonly \
  "$TEAM_IMAGE" python -m app.operations.team_backup restore \
  --data-root /srv/forgehand-recovery/data --backup /backups/ensaio-001 \
  --database-url-env RESTORE_DATABASE_URL
```

Não crie `/srv/forgehand-recovery/data` antes do comando. O UID deve corresponder
ao proprietário registrado no backup. Mantenha executores parados até conferir
manifesto, dados e aprovações pendentes.
Leases/checkpoints contêm caminhos absolutos: o marcador `.restore-info.json`
bloqueia a execução em um caminho diferente do original. A opção `--original-path`
permite recuperar a execução somente no caminho absoluto registrado no backup.
Nesse caso, use uma instalação original parada ou host isolado, mantenha esse
destino inexistente e monte **seu pai** no `docker run`, com a menor abrangência
possível; substitua `--data-root` pelo caminho original e acrescente
`--original-path`. Não monte diretamente o destino ausente nem use um mount que
o crie. O banco ainda precisa ser distinto e vazio. Depois de uma restauração
concluída, configure `DATABASE_URL` da instalação para o banco restaurado antes
de iniciar API/workers com a revisão, perfis e caminhos originais. Não reescreva
caminhos automaticamente nem ative workers contra um backup para apenas inspecioná-lo.
Não use `docker compose down --volumes` como procedimento de manutenção.

Para atualização coordenada, interrompa novas submissões e drene entregas,
fluxos genéricos e decisões pendentes da instalação inteira **antes** de mudar
revisão, fontes, perfis, caminhos, socket ou backends. Depois pare API/workers,
preserve backup verificável, prepare uma única imagem/revisão/configuração nova
e recrie os três processos. Jobs incompatíveis, antigos sem fingerprint
(`legacy_unbound`) ou sem configuração não são migrados nem executados
automaticamente: o diagnóstico retorna `jobs_require_reconciliation`. Uma
retomada conserva o fingerprint do workflow original. A proteção de
compatibilidade ativada no banco é permanente; desligar factory mode não é um
procedimento de migração.
A compatibilidade entre API e workers e `/readyz` devem voltar a passar antes de
reabrir o uso da equipe. Mantenha a imagem anterior e sua configuração para uma
reversão coordenada; a reversão de código não reverte efeitos já publicados no GitHub.


## Reproduzir o ensaio de instalação

Em um host Linux de testes com Docker/Compose, prepare o ambiente Python do projeto
(`uv sync --extra dev --extra postgres --extra github-app --locked`) e execute:

```bash
docker build --target runtime --build-arg FORGEHAND_REVISION="$(git rev-parse HEAD)" --tag forgehand:team-test .
docker pull python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
RUN_TEAM_INSTALLATION_TESTS=1 FORGEHAND_TEAM_TEST_IMAGE=forgehand:team-test \
  uv run pytest tests/integration/test_team_installation.py tests/unit/test_team_compose.py -q
```

O ensaio cria um projeto Compose e bancos descartáveis próprios, inicia a API e
dois workers, mata o worker que está executando, verifica retomada e build real,
faz backup frio e restaura em outro banco. Confirma que histórico, owner,
idempotência, orçamento e aprovação pendente sobrevivem e que a conclusão só
ocorre após decisão explícita. O grafo é determinístico e bloqueia chamadas HTTP
a LLM/GitHub; este ensaio mede operação, não a qualidade de mudanças geradas por IA.
O cleanup remove somente os recursos criados pelo teste. O job `team-installation`
executa esse cenário em CI. Docker Desktop pode servir para ensaio local, mas o
perfil operacional suportado continua sendo Linux com Docker Engine local.
