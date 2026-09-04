# Aplicação full-stack · base Forgehand

Este pacote inclui backend, login e registros persistentes. Não precisa de
Forgehand nem de uma chave de IA para funcionar. `brief.json` registra a ideia
aprovada da demo original; este pacote acrescenta a infraestrutura descrita aqui,
mas não garante implementar todos os requisitos semânticos daquele briefing.

## Primeiro uso local — Python 3.12+

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export APP_ENV=development
export APP_ORIGIN=http://127.0.0.1:8000
export DATABASE_URL=sqlite:///data/product.sqlite3
python -m runtime.manage migrate
python -m runtime.manage create-user --username recepcao
uvicorn runtime.server:from_environment --factory --host 127.0.0.1 --port 8000 --no-proxy-headers --no-access-log
```

A senha é solicitada sem eco (12–128 caracteres). Não há credencial padrão nem
cadastro público. Abra a origem exata configurada, entre e crie um registro.
Feche/reabra o navegador: os dados continuam no banco. `localhost` e `127.0.0.1`
são origens diferentes; use a configurada. SQLite serve apenas para desenvolvimento.

## PostgreSQL e container

Copie `.env.example` para `.env` e configure uma senha própria, a URL PostgreSQL e
a origem. Nunca versione `.env`, bancos ou backups. No Compose, o host do banco é
`db`, usuário/banco `product`; faça percent-encoding da senha na URL.

```bash
docker compose up -d db
docker compose build app
docker compose run --rm app python -m runtime.manage migrate
docker compose run --rm app python -m runtime.manage create-user --username recepcao
docker compose up -d app
```

Para ensaio local com PostgreSQL, use APP_ENV=development e origem HTTP local.
Produção exige PostgreSQL e origem HTTPS: configure um proxy TLS na frente da API
antes de atender usuários. A porta da API fica vinculada ao loopback, e o banco não
é publicado. O container é não root/read-only e a API não confia em cabeçalhos de
proxy por padrão. Revise hosts/proxy/TLS, segredos, limites e monitoramento antes
de qualquer exposição pública. Dependências diretas são fixadas; imagem base e
dependências transitivas ainda devem ser fixadas por digest/lock no seu pipeline.

## Uso e limites

- Criar, editar, excluir com confirmação, buscar (Enter ou Atualizar lista), paginar
  e exportar JSON. Até 100 registros por página na API, 25 na interface; exportação
  limitada a 1000 registros por usuário. Há limite de corpo de 16 KiB.
- Dados privados por usuário. **Não há equipe compartilhada, permissões por papel,
  disponibilidade de agenda ou prevenção de dois agendamentos no mesmo horário.**
- Edição/exclusão exige a versão atual. Em conflito, copie suas mudanças, use
  Atualizar lista e edite a versão nova; nada é sobrescrito silenciosamente.
- Senhas scrypt (N=2^17, r=8, p=1), sessões de oito horas, cookies HttpOnly e
  SameSite=Strict; Secure obrigatório em produção. Sessões ficam no banco como hash.
  Logout revoga a sessão; reset-password revoga todas as sessões do usuário.
- Mutações exigem JSON e Origin igual a APP_ORIGIN. Clientes HTTP próprios também
  precisam enviar esse cabeçalho e guardar o cookie. Não existe CORS permissivo.
- Login tem janela fixa de 15 minutos: 10 tentativas por usuário e 30 por IP,
  incluindo logins válidos. Atrás de proxy sem configuração de IP confiável, todos
  compartilham o limite do IP do proxy. Não libere cabeçalhos encaminhados de qualquer
  origem; ajuste essa integração e os limites ao preparar produção.
- Modelo congelado pelo hash da migração. Não edite model.json depois de haver
  dados sem desenhar uma migração. Readiness/startup recusam schema/modelo diferente.
- Cada processo tem pool PostgreSQL de até oito conexões. Sessões e limites são
  compartilhados no banco, mas isso **não certifica escalabilidade**. Meça a carga
  real, dimensione o banco e ajuste limites antes de escalar réplicas. Busca textual
  é limitada/paginada, mas ainda usa LIKE; grandes volumes exigem índices dedicados.
- Sem e-mail, MFA, SSO, fila de trabalhos, pagamentos, implantação pública ou
  certificação automática de segurança. Registros de exemplo da demo não são importados.

## Operação, backup e recuperação

`GET /healthz` verifica processo; `GET /readyz` verifica banco e versão/hash. Observe
erros 5xx, latência p95, conexões, disco e falhas de login no seu monitoramento;
não registre corpos de login, cookies nem dados pessoais. O comando recomendado
desativa access logs para não gravar termos de busca na URL; configure telemetria
com rotas normalizadas, códigos e tempos, sem parâmetros ou corpos.

```bash
# Limpeza periódica de sessões expiradas e contadores antigos
docker compose exec app python -m runtime.manage cleanup
# Recuperação de acesso local pelo operador; invalida sessões anteriores
docker compose exec app python -m runtime.manage reset-password --username recepcao
# Backup PostgreSQL. Proteja/permissões e criptografe o arquivo fora do servidor.
docker compose exec -T db pg_dump -U product -d product -Fc > product.dump
```

Ensaie restauração **em um banco vazio separado**, nunca sobre o original:
`pg_restore --no-owner --dbname=URL_DO_BANCO_DE_ENSAIO product.dump`. A senha não
deve ir na linha de comando; use o mecanismo seguro do cliente PostgreSQL. Aponte
uma instância isolada da aplicação para a cópia e verifique readiness, login e
registros. A política de retenção e o agendamento são responsabilidade do operador.
Em SQLite de desenvolvimento, pare a API e copie o arquivo do banco; restaure
essa cópia em outro caminho para testar.

Antes de atualizar: backup, ensaio de restauração, migração em staging e verificação
de compatibilidade. A migração v1 é explícita e idempotente; não há downgrade
destrutivo automático. Faça rollback do código somente com schema compatível.

`manifest.json` identifica o runtime e os hashes dos arquivos gerados. Ele verifica
integridade acidental, não é uma assinatura digital. Rode os testes do Forgehand
para validar o motor; valide também os requisitos do seu produto antes da entrega.

Referência de armazenamento de senhas: [OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
