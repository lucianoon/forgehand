# Checkout de repositórios privados

O modo fábrica usa a credencial GitHub do servidor para clonar e atualizar
repositórios privados em `github.com`. A mesma configuração já usada na
publicação de PRs agora atende o checkout; a CLI recebe somente a API key do
Forgehand. Factory mode continua desabilitado por padrão.

## Configurar

Use `GITHUB_TOKEN` com acesso aos repositórios aprovados ou configure
`GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID` e `GITHUB_APP_PRIVATE_KEY_PATH`
(ou `GITHUB_APP_PRIVATE_KEY`). Uma configuração completa de GitHub App tem
prioridade sobre o token estático. Instale `uv sync --extra github-app` para
usar a App. API e workers dedicados precisam da configuração no servidor.

Para checkout, a credencial precisa ler o conteúdo do repositório. Publicação
exige também as permissões de escrita e leitura de checks descritas em
[integrações](integrations.md). Tokens de instalação suportam Git sobre HTTP
com permissão Contents, conforme a [documentação do GitHub](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation).

Envie a ordem normalmente por `forgehand deliver --repository owner/repo` ou
pelo dashboard. Veja o [guia da CLI](developer-delivery-cli.md) para critérios,
perfil de build, orçamento e acompanhamento. Nomes e destino precisam
corresponder à URL canônica `https://github.com/owner/repo.git`; após renomear
ou transferir o repositório, atualize a ordem. Redirecionamentos são recusados.

## Credenciais e retomada

O provedor é consultado antes de cada operação Git remota. O provedor de App
reutiliza seu token enquanto válido e solicita outro quando faltam menos de
60 segundos para expirar. Tokens estáticos são lidos na inicialização;
para trocá-los, reinicie API e workers de forma coordenada. Não há fallback
anônimo após falha de autenticação nem repetição automática com outra credencial.

A autorização é revalidada antes de retomar uma lease, inclusive quando o
checkout já existe. Remover a configuração, revogar acesso ou perder conexão
impede a retomada de código privado; o workspace e o checkpoint permanecem
preservados para diagnóstico e recuperação. Isso não interrompe retroativamente
uma tarefa já em execução entre duas operações remotas.

O token é entregue somente no ambiente do subprocesso Git remoto, em um header
limitado ao destino. Não é escrito na URL, nos argumentos, no cache, no checkout,
no journal ou no ambiente global, e não é repassado aos comandos locais ou builds.
Saídas com o token cru ou seu Basic codificado são redigidas antes do truncamento.
A implementação usa a [configuração de runtime do Git](https://git-scm.com/docs/git-config#Documentation/git-config.txt-GITCONFIGCOUNT), sem helper de credenciais em disco.

O transporte verifica TLS, desabilita redirects e não carrega configuração Git
global ou de um repositório ancestral. Configurações inesperadas no cache
(includes, rewrite de URL, proxy, hooks ou mudança de origem) são recusadas
antes de obter a credencial. O cache é administrado pelo Forgehand; não adicione
configuração Git manual nele. Um erro de configuração exige inspeção do operador,
sem descarte automático de dados. No encerramento, workers param antes do cliente
de tokens ser fechado.

## Alcance e validação

O credenciamento é exclusivo de `github.com`; a allowlist de hosts não autoriza
enviar o token GitHub a outros provedores. Não há suporte nesta entrega a SSH,
GitHub Enterprise, submódulos privados ou autenticação de dependências privadas.
Repositórios públicos continuam acessíveis sem credenciais.

O escopo por projeto da API **não constitui uma ACL por repositório**. A
instalação usa uma credencial de servidor e cache compartilhado por repositório;
limite essa credencial aos repositórios da equipe. Equipes que não confiam umas
nas outras precisam de isolamento adicional antes de compartilhar instalação.
O código privado permanece no disco administrado pelo servidor, sujeito à
retenção e limpeza de workspaces; a limpeza da lease preserva o cache.

Testes automatizados usam Git real com servidor HTTPS local e credenciais
fictícias para clone, fetch, rejeição de acesso, redirects e TLS. Outros testes
cobrem rotação do provedor, retomada com cache, adulteração de configuração e
lifecycle. Isso não equivale a um piloto em um repositório privado real do
GitHub; esse piloto continua sendo uma etapa operacional antes da adoção.

```sh
uv run pytest -q tests/integration/test_private_git_transport.py \
  tests/unit/test_private_repository_workspace.py tests/unit/test_git_auth.py \
  tests/unit/test_container_repository_access.py
```

A integração exige Git, OpenSSL e permissão para abrir um servidor HTTPS local.
Não usa LLM nem credenciais reais.
