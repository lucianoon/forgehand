# Política de segurança

## Versões suportadas

O branch `main` recebe correções de segurança. Releases e commits antigos não
recebem backports garantidos.

## Reportar uma vulnerabilidade

Não abra uma issue pública. Use **Security → Report a vulnerability** neste
repositório para enviar o relato de forma privada.

Inclua, quando possível:

- componente e versão/commit afetado;
- cenário de exploração e impacto;
- passos mínimos para reprodução;
- mitigação sugerida, se houver.

O objetivo é confirmar o recebimento em até 3 dias úteis e publicar uma
avaliação inicial em até 7 dias úteis.

## Escopo sensível

Relatos sobre execução de comandos, isolamento de workspace, injeção de prompt,
exposição de segredos, autorização/RBAC, webhooks e vazamento de dados de
tracing são especialmente relevantes.

Nunca inclua chaves reais, dados pessoais ou segredos de produção no relato.
