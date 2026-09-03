# Executor de fases da fábrica

`app.factory.sandbox.DockerBuildRunner` executa um perfil administrado a partir
da seleção persistida e de uma lease ativa. A execução é sequencial e para na
primeira falha. Em factory mode, o grafo executa uma tarefa por vez e roda o
perfil depois de cada alteração, antes do judge. Configurar perfis válidos e
habilitar o modo fábrica ativa esse caminho; workflows legados não o utilizam.

## Contrato de execução

- O fingerprint da seleção precisa corresponder ao perfil atual.
- Cada fase é reautorizada imediatamente antes de executar, incluindo cwd e
  argumentos que possam ter sido alterados por symlinks na preparação.
- Imagens precisam estar instaladas e fixadas por digest. Não há pull automático.
- A imagem deve incluir `/usr/bin/env` e os executáveis absolutos do perfil.
  ENTRYPOINT, CMD e HEALTHCHECK da imagem não determinam o comando executado.
- Imagens que declarem VOLUME são recusadas: a única montagem de arquivos do
  host é a lease. O filesystem raiz é read-only; `/tmp` tem tmpfs limitado.
- O processo usa UID/GID não-root, capabilities removidas, no-new-privileges,
  limites de memória (sem swap extra), CPU, processos, descritores e tempo.
  O diretório da lease precisa ser gravável pelo UID/GID do controlador.
- A rede fica desligada. Somente `prepare` pode usar bridge, e apenas quando o
  operador constrói o runner com `allow_dependency_network=True`. Essa opção
  não concede rede às fases de validação e não implementa allowlist de destinos.
- O processo de build recebe um ambiente limpo, com PATH/HOME fixos e as
  variáveis permitidas do perfil. Credenciais de SCM/LLM, proxies, Docker
  context e variáveis do host não são repassados pelo cliente.
- O daemon Docker local é uma dependência privilegiada e deve ser administrado
  pelo operador. O runner usa um socket Unix explícito, não hosts remotos.

Os controles usam as opções documentadas de
[docker create](https://docs.docker.com/reference/cli/docker/container/create/)
e [docker run](https://docs.docker.com/reference/cli/docker/container/run/).

## Resultados e limpeza

`BuildRunResult` contém identidade/fingerprint do perfil e resultados ordenados
das fases. Os resultados diferenciam sucesso, falha do comando, rejeição de
política, timeout, limite de recurso e falha de infraestrutura. OOM só é
classificado quando o Docker confirma `OOMKilled`; exit code 137 sozinho não
comprova falta de memória. Os demais limites continuam ativos, mas nem todo
erro do programa causado por limites pode ser distinguido de falha comum.

A saída é drenada continuamente e limitada durante a captura, não apenas
depois de acumular toda a resposta. O runner remove controles de terminal e
pode receber valores conhecidos em `redacted_values` para redigi-los. Isso
não é um detector genérico de segredos; a integração de evidências sanitizadas
também recebe os valores conhecidos das credenciais SCM/LLM do controlador.
O relatório tipado é anexado à tentativa, ao resultado da tarefa, ao status,
ao input do judge, à auditoria e ao resumo final sem incluir essas credenciais.

Uma fase obrigatória reprovada é veto estrutural: o judge não pode aprovar a
tarefa. A saída limitada e sanitizada entra no feedback da próxima tentativa,
que continua sujeita aos budgets de tarefa e workflow. Se o runner não estiver
disponível em um workflow de fábrica, o resultado é a falha fechada
`sandbox_runner_unavailable`.

No cancelamento, `BuildRunCancelled` mantém a semântica de `CancelledError` e
carrega um relatório tipado. O caller só recebe o cancelamento após a tentativa
limitada de remoção do container. A remoção verifica um token de ownership;
containers com outro token nunca são removidos.

Se a remoção falhar ou uma criação interrompida ficar incerta, o workflow fica
em quarentena nesse runner. Novas execuções são recusadas até
`retry_cleanup(workflow_id)` confirmar a limpeza. `active_containers` informa
os nomes para investigação. Essa quarentena é local ao processo: persistência
de lifecycle e reconciliação após restart ainda pertencem às tarefas 4.1–4.4.
Não libere a lease quando o relatório indicar `cleanup_failed`.

## Validação

Os testes unitários exercitam política, flags, sequência, erros, OOM, captura
limitada, cancelamento e quarentena usando um cliente Docker controlado. Há
também testes opt-in com containers reais:

```sh
RUN_FACTORY_DOCKER_TESTS=1 \
FACTORY_DOCKER_SOCKET=/var/run/docker.sock \
FACTORY_DOCKER_PYTHON_TEST_IMAGE='imagem-python@sha256:DIGEST_REAL' \
FACTORY_DOCKER_NODE_TEST_IMAGE='imagem-node@sha256:DIGEST_REAL' \
uv run pytest tests/integration/test_factory_sandbox_docker.py -q
```

Substitua as referências esquemáticas por digests reais de imagens já
instaladas e aprovadas. Os testes não baixam imagens e não acessam credenciais.
Sem opt-in ou sem a imagem do ecossistema, os casos são ignorados. Ainda faltam
qualificação real de timeout/OOM/cancelamento e fixtures versionadas antes de
considerar o sandbox qualificado para produção.

A publicação e o gate de revisão estão descritos em
[entrega validada da fábrica](factory-delivery.md).
