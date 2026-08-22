# Modelo de segurança resumido

## Fronteiras de confiança

- conteúdo do repositório e respostas de modelos são dados não confiáveis;
- comandos só atravessam uma allowlist e devem rodar no sandbox em produção;
- a allowlist pressupõe execução em forma exec (argv), nunca via shell — é o
  que garante que o executável validado seja o executável que roda. Nenhum
  runner interpreta a string do comando com um shell;
- tudo que a tarefa executa no workspace usa o mesmo runner, incluindo o
  snapshot de `git status`/`git diff`: com `EXECUTOR_COMMAND_BACKEND=docker`,
  nada escapa para o host;
- segredos vêm do ambiente e nunca entram em prompts ou `Settings`;
- mutações SCM exigem role `approver` e ficam registradas na auditoria;
- webhooks são autenticados com HMAC-SHA256.

## Controles implementados

- isolamento de paths contra traversal;
- Docker sem rede por padrão, sem capabilities e sem novos privilégios;
- limites de CPU, memória e processos;
- RBAC e escopo por projeto/workflow;
- headers HTTP de hardening e request ID;
- audit log persistente JSONL para single-node;
- budgets, timeout, lease ownership e gate humano;
- structured outputs e evidências grounded.

## Riscos residuais antes de produção enterprise

- JSONL não substitui auditoria imutável centralizada;
- API keys devem migrar para identidade OIDC/SAML;
- o sandbox Docker precisa de imagem versionada, scan e validação em host real;
- a imagem do sandbox precisa conter os executáveis da allowlist que a pipeline
  usa. Sem `git` na imagem, o snapshot é omitido (degradação de
  observabilidade, não falha da tarefa);
- é necessário pentest, egress policy no cluster e secret scanning;
- multi-tenant forte exige banco, storage e compute segregados.
