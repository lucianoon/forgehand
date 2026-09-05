# Backup e restauração da instalação de equipe

O procedimento captura PostgreSQL em formato custom e todo o `FORGEHAND_DATA_ROOT`, incluindo workspaces, cache Git, journal SQLite e auditoria. API e workers precisam estar parados. O PostgreSQL permanece ligado. Este procedimento corresponde à instalação de um único host descrita em [team-installation.md](team-installation.md).

## Contrato operacional

- API e workers devem iniciar pelo wrapper `python -m app.operations.team_backup run --data-root ABSOLUTO -- COMANDO`. O Compose fornecido já faz isso. O wrapper recusa caminhos relativos ou com componentes simbólicos e mantém um `flock` compartilhado em `.maintenance.lock` durante o processo e os filhos Git/Docker gerenciados.
- Backup exige o lock exclusivo, todos os locks de workspace livres e nenhuma entrada de container pendente no journal. Também verifica ausência de outras sessões cliente no banco antes e depois do dump. Falha antes de criar o bundle quando encontra processos ativos nessas verificações. Finalize a limpeza de sandboxes antes de parar os serviços; não apague registros do journal para contornar essa recusa.
- Pare também clientes administrativos e qualquer processo externo que possa escrever no banco ou nos arquivos. O lock é cooperativo: aplicações iniciadas fora do wrapper e escritores externos no filesystem não são completamente detectáveis. Mantenha a instalação em manutenção até o backup terminar.
- Use PostgreSQL 16 e clientes `pg_dump`/`pg_restore` compatíveis. A imagem de equipe inclui os clientes 16. O usuário PostgreSQL precisa ler o banco inteiro e consultar sessões para o backup; no destino, precisa criar os objetos restaurados. O administrador cria o banco destino vazio separadamente.
- O destino do backup deve ser um diretório novo, fora dos dados. O bundle tem modo `0700`; manifest, dump e arquivo de dados têm modo `0600`. A conexão vem de uma variável de ambiente, nunca de argumento de linha de comando. Especifique um único host (ou hostaddr), porta, usuário e banco; conexões com failover multi-host e nomes de banco interpretáveis como connection string são recusados; variáveis `PG*` herdadas devem estar ausentes para que inspeção e ferramentas usem exatamente a mesma conexão. `DATABASE_URL` e `RESTORE_DATABASE_URL` não são variáveis `PG*`. Respostas do servidor e segredos não são impressos nos erros.

## Criar o backup

Use o arquivo privado de configuração já preparado na instalação. Pare somente API e workers, deixando o banco ligado:

```sh
docker compose --env-file "$TEAM_ENV_FILE" -f docker-compose.team.yml stop api worker
```

No host, com os clientes PostgreSQL e as dependências Python instalados, uma conexão administrativa `DATABASE_URL` já carregada no ambiente e acesso ao banco:

```sh
scripts/team-backup backup \
  --data-root "$FORGEHAND_DATA_ROOT" \
  --output "$BACKUP_PARENT/backup-2026-09-05" \
  --database-url-env DATABASE_URL
```

O Compose não publica PostgreSQL no host. Para executar na rede interna, use um container de manutenção da mesma imagem. Configure `TEAM_NETWORK`, `FORGEHAND_IMAGE`, `FORGEHAND_UID`, `FORGEHAND_GID` e os caminhos absolutos; o arquivo `BACKUP_ENV_FILE`, fora dos dados e com modo `0600`, contém somente a conexão `DATABASE_URL` necessária. Crie antes `BACKUP_PARENT`, com proprietário igual ao UID do runtime e modo `0700`:

```sh
docker run --rm --network "$TEAM_NETWORK" \
  --user "$FORGEHAND_UID:$FORGEHAND_GID" \
  --env-file "$BACKUP_ENV_FILE" \
  --mount "type=bind,src=$FORGEHAND_DATA_ROOT,dst=$FORGEHAND_DATA_ROOT" \
  --mount "type=bind,src=$BACKUP_PARENT,dst=/backups" \
  "$FORGEHAND_IMAGE" python -m app.operations.team_backup backup \
  --data-root "$FORGEHAND_DATA_ROOT" --output /backups/backup-2026-09-05
```

Esse container não precisa do socket Docker nem de credenciais de LLM/GitHub. `TEAM_NETWORK` é a rede real do projeto Compose; confirme seu nome na instalação. A conexão deve apontar ao serviço PostgreSQL nessa rede. Não habilite shell tracing ao carregar arquivos de conexão.

Sucesso imprime JSON com versão do formato, revisão da aplicação, UID/GID original, caminho dos dados, nome do banco, hashes SHA-256 e inventário. O manifest é escrito por último: artefatos de uma tentativa incompleta sem manifest válido não são restauráveis. Depois de confirmar o sucesso, reinicie os serviços usando os mesmos parâmetros de escala da instalação e confirme `forgehand doctor`.

## Restaurar e ensaiar

A restauração exige **banco recém-criado, vazio, sem outras sessões e com nome diferente da origem**, além de **diretório de dados inexistente cujo parent já existe**. Não limpa tabelas, não usa `--clean`, não substitui dados e não cria bancos automaticamente. Os hashes e todos os caminhos/tipos/links do arquivo são validados antes de criar o destino; o dump precisa ser reconhecido por `pg_restore --list`. O SQL é restaurado em uma única transação.

Use somente bundles confiáveis da própria instalação. Hashes verificam integridade, não autenticidade; um dump PostgreSQL pode conter SQL executável. Proteja a cópia e seu manifest juntos. Esses arquivos não são criptografados pelo comando.

1. Prepare um host isolado e o novo banco. Reponha a configuração e as credenciais por um canal separado, com a mesma revisão da aplicação usada no backup. A conexão destino vai em `RESTORE_DATABASE_URL` ou no nome escolhido em `--database-url-env`.
2. Para inspeção offline, escolha um novo caminho absoluto diferente da origem. O recibo retorna `runtime_path_matches=false`. O wrapper recusa iniciar serviços nesse diretório: checkpoints e journal mantêm os caminhos absolutos originais, sem reescrita.
3. Para retomar trabalho, use o **mesmo caminho absoluto original em host isolado**, ainda inexistente, com `--original-path`. A origem deve permanecer offline. Esse opt-in não permite sobrescrever um diretório existente nem restaurar sobre o banco de origem. O operador é responsável pelo isolamento do host e pela escolha do banco.

Exemplo no host:

```sh
scripts/team-backup restore \
  --data-root "$RESTORE_DATA_ROOT" \
  --backup "$BACKUP_PARENT/backup-2026-09-05" \
  --database-url-env RESTORE_DATABASE_URL \
  --original-path
```

Em Docker, monte o **parent** existente do destino: montar o próprio diretório ausente pode criá-lo antes do comando e impedir a restauração. Não use `compose run worker` para restaurar o caminho original ausente, pois seu bind do data root exige existência. `RESTORE_PARENT` deve ser um diretório dedicado, com espaço para o novo root; `RESTORE_ENV_FILE` contém somente a conexão destino:

```sh
docker run --rm --network "$TEAM_NETWORK" \
  --user "$FORGEHAND_UID:$FORGEHAND_GID" \
  --env-file "$RESTORE_ENV_FILE" \
  --mount "type=bind,src=$RESTORE_PARENT,dst=$RESTORE_PARENT" \
  --mount "type=bind,src=$BACKUP_PARENT/backup-2026-09-05,dst=/backup,readonly" \
  "$FORGEHAND_IMAGE" python -m app.operations.team_backup restore \
  --data-root "$RESTORE_DATA_ROOT" --backup /backup --original-path
```

Execute como UID original ou root. Root repõe UID/GID registrado; arquivos regulares recebem `0600`, executáveis `0700` e diretórios `0700`. Links simbólicos internos seguros são preservados; links que escapam dos dados, ciclos, dispositivos, sockets e caminhos reservados são recusados. Hardlinks são restaurados como arquivos independentes com os mesmos bytes.

Uma falha depois de criar o destino deixa `.restore-in-progress` e impede inicialização pelo wrapper. Preserve esse destino para investigação e repita em **outro banco novo e outro diretório ausente**. Não remova o marcador para forçar startup. Concluída a restauração operacional, ajuste a configuração para o banco novo, mantenha o caminho dos dados original e valide saúde, histórico, proprietário e aprovações pendentes antes de liberar tráfego. Restauração não concede aprovação: a decisão humana continua necessária.

## Conteúdo sensível e limites

Todos os arquivos do data root são preservados, inclusive `.env`, `.env.example` e arquivos versionados: remover esses arquivos alteraria o checkout recuperado. Somente `.maintenance.lock`, `.restore-in-progress` e `.restore-info.json` na raiz são excluídos; nomes iguais dentro dos projetos permanecem no snapshot. **Não existe exclusão ou varredura de segredos:** arquivos, banco, auditoria e histórico Git podem conter dados privados ou segredos que já tenham sido gravados. Mantenha credenciais operacionais fora do data root e armazene o bundle com os mesmos controles de acesso dos dados originais. Volumes externos, imagens Docker, configuração do host e segredos do servidor não são incluídos.

## Ensaio automatizado

`tests/integration/test_team_backup_roundtrip.py` usa `PostgresWorkflowQueue`, o checkpointer PostgreSQL e `WorkflowService` reais, com um grafo local que para em aprovação humana. Cria dois bancos com nomes UUID, executa `pg_dump`/`pg_restore`, restaura journal/auditoria e confirma owner, histórico de checkpoints, identidade da fila, idempotência, checkout Git limpo com `.env`/`.env.example` versionados, aprovação pendente e conclusão somente após nova decisão explícita. A origem continua pendente e não é modificada. O teste não usa GitHub, LLM nem banco de produção.

```sh
RUN_TEAM_BACKUP_TESTS=1 python -m pytest -q tests/integration/test_team_backup_roundtrip.py
```

Configure previamente `TEST_DATABASE_URL` para um PostgreSQL de testes cujo usuário pode criar/remover bancos. Se necessário, `TEAM_BACKUP_PG_DUMP` e `TEAM_BACKUP_PG_RESTORE` escolhem executáveis de cliente. Os únicos bancos removidos pelo teste são os dois que ele próprio criou. Sem opt-in, conexão ou clientes, o ensaio é explicitamente ignorado.
