# Modelo de segurança resumido

## Fronteiras de confiança

- conteúdo do repositório e respostas de modelos são dados não confiáveis;
- comandos só atravessam uma allowlist e devem rodar no sandbox em produção;
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
- structured outputs e evidências grounded;
- checkout privado com token efêmero, destino validado e autorização reconsultada
  na retomada; [escopo e limites](private-repositories.md).

## Riscos residuais antes de produção enterprise

- JSONL não substitui auditoria imutável centralizada;
- API keys devem migrar para identidade OIDC/SAML;
- o sandbox Docker precisa de imagem versionada, scan e validação em host real;
- é necessário pentest, egress policy no cluster e secret scanning;
- multi-tenant forte exige banco, storage e compute segregados.
