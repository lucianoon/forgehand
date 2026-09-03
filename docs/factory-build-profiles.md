# Perfis administrados de build

Os perfis de build são configuração do operador do Forgehand. O repositório e
os agentes não podem criar um perfil nem ampliar seus comandos permitidos.
Cada perfil associa um ecossistema, uma imagem fixada e uma sequência de fases
a comandos aprovados explicitamente.

O Forgehand entrega a seleção, a autorização e um
[executor Docker de fases](factory-sandbox.md). Em factory mode, as fases são
executadas depois de cada alteração e antes do judge; falhas obrigatórias
reabrem a tarefa para uma tentativa limitada e impedem aprovação.

Com `FACTORY_MODE_ENABLED=true`, o backend aceito é obrigatoriamente `docker`;
execução local permanece disponível somente para workflows legados. O modo
fábrica não reutiliza os comandos string legados de pytest, Ruff ou mypy como
fallback.

## Configuração do operador

`FACTORY_BUILD_PROFILES_JSON` recebe um objeto JSON que mapeia o nome do perfil
para sua definição. A chave injeta o campo `name`; não é necessário repeti-lo
no objeto. Os nomes devem começar com letra minúscula e conter somente letras
minúsculas, números, `_` ou `-`, com no máximo 64 caracteres.

`FACTORY_REPOSITORY_PROFILES_JSON` recebe um objeto JSON que mapeia
`owner/repo` para o nome de um perfil configurado. Esse mapeamento é mantido
pelo operador, não por um arquivo do checkout.

Um perfil contém:

| Campo | Significado |
| --- | --- |
| `ecosystem` | `python` ou `node`. |
| `image` | Referência de imagem terminada em `@sha256:` e um digest real de 64 caracteres hexadecimais minúsculos. Tags isoladas não são aceitas. |
| `phases` | De uma a cinco fases, na ordem em que foram aprovadas pelo operador. |
| `auto_detect` | Padrão `false`. Autoriza o uso do perfil pela detecção segura do ecossistema. |

A detecção só seleciona quando existe exatamente um perfil com
`auto_detect: true` para o ecossistema encontrado; vários candidatos resultam
em `unsupported`. É possível manter outros perfis desse ecossistema para
seleção explícita ou mapeamento de repositórios.

O operador deve fornecer e verificar a imagem e seu digest real, incluindo os
executáveis e as dependências que o perfil exige. O validador verifica o
formato da referência; isso não comprova disponibilidade, conteúdo ou
confiabilidade da imagem.

## Campos de uma fase

| Campo | Regra |
| --- | --- |
| `name` | Um de `prepare`, `build`, `test`, `lint` ou `types`. Cada nome aparece no máximo uma vez. Se houver `prepare`, deve ser a primeira fase. |
| `argv` | Lista de 1 a 64 tokens. O primeiro é um caminho absoluto e normalizado para um executável aprovado da imagem; os demais são argumentos explícitos. |
| `cwd` | Padrão `.`. Diretório relativo, normalizado e dentro da lease; caminhos absolutos e segmentos `..` são rejeitados. |
| `environment` | Padrão `{}`. Somente as variáveis permitidas abaixo; não pode transportar credenciais. |
| `network` | Padrão `none`. `dependencies` só pode ser solicitado na fase `prepare`; a solicitação não concede acesso à rede por si só. |
| `timeout_seconds` | Padrão 120; de 1 a 3600 segundos. |
| `output_limit` | Padrão 12000; de 256 a 100000 para o limite de saída capturada. |

Os nomes de executáveis permitidos são `python`, `python3`, `pytest`, `ruff`,
`mypy`, `uv`, `node`, `tsc` e `eslint`. O caminho deve apontar para a imagem,
por exemplo `/usr/local/bin/python`, nunca depender da resolução de `PATH` do
checkout ou começar com `/workspace/`. O caminho exato precisa existir na
imagem escolhida.

As variáveis de ambiente permitidas são `CI`, `LANG`, `LC_ALL`, `TZ`,
`PYTHONDONTWRITEBYTECODE`, `PYTHONHASHSEED`,
`PYTEST_DISABLE_PLUGIN_AUTOLOAD`, `NODE_ENV`, `NO_COLOR` e `FORCE_COLOR`.
Chaves como tokens SCM ou de provedores de IA não são permitidas.

Os comandos são listas de argumentos, não trechos de shell. Tokens vazios,
excessivamente longos, caracteres de controle e sintaxe como `;`, `&&`, pipes,
redirecionamentos, crases e expansão com `$` são rejeitados. A autorização
compara o comando completo e os demais campos da fase com o perfil selecionado;
permitir o nome de um executável não autoriza argumentos arbitrários. Scripts
encontrados no repositório não se tornam comandos aprovados automaticamente.

Para evitar interpretações ambíguas, caminhos colados em flags curtas
(`-I/tmp`) e atribuições compostas (`--override-ini=cache_dir=cache` ou
`cache_dir=cache`) não são suportados. A inspeção de argumentos não analisa
todo o comportamento do programa executado nem impede alterações posteriores
no checkout: o sandbox continua sendo obrigatório para conter os acessos
efetuados pelo código do repositório.

## Exemplos esquemáticos

Os exemplos abaixo **não são perfis executáveis prontos**.
`<DIGEST_REAL_DA_IMAGEM>` é um placeholder deliberadamente inválido para a
configuração: substitua-o por um digest real de 64 caracteres hexadecimais
minúsculos, fornecido e verificado pelo operador. Substitua também o nome da
imagem por uma imagem que contenha o executável indicado e tudo que os testes
precisam. Não use um digest inventado.

Conteúdo esquemático de `FACTORY_BUILD_PROFILES_JSON`:

```json
{
  "python-unittest": {
    "ecosystem": "python",
    "image": "registry.example.com/forgehand/python@sha256:<DIGEST_REAL_DA_IMAGEM>",
    "auto_detect": true,
    "phases": [
      {
        "name": "test",
        "argv": ["/usr/local/bin/python", "-m", "unittest", "discover", "-s", "tests"],
        "cwd": ".",
        "environment": {"CI": "true", "PYTHONDONTWRITEBYTECODE": "1"},
        "network": "none",
        "timeout_seconds": 120,
        "output_limit": 12000
      }
    ]
  },
  "node-test": {
    "ecosystem": "node",
    "image": "registry.example.com/forgehand/node@sha256:<DIGEST_REAL_DA_IMAGEM>",
    "auto_detect": true,
    "phases": [
      {
        "name": "test",
        "argv": ["/usr/local/bin/node", "--test"],
        "cwd": ".",
        "environment": {"CI": "true", "NODE_ENV": "test"},
        "network": "none",
        "timeout_seconds": 120,
        "output_limit": 12000
      }
    ]
  }
}
```

Conteúdo esquemático de `FACTORY_REPOSITORY_PROFILES_JSON`:

```json
{
  "minha-organizacao/servico-python": "python-unittest",
  "minha-organizacao/servico-node": "node-test"
}
```

Esses comandos exemplificam os test runners da biblioteca padrão de Python e
do Node. Eles não instalam dependências nem garantem que sejam adequados a
qualquer repositório Python ou Node. O operador deve aprovar fases compatíveis
com os testes e critérios de aceitação do projeto.

## Seleção e retomada

A precedência é determinística:

1. Perfil explicitamente solicitado pela work order
   (`build_profile.requested_profile`).
2. Mapeamento administrado para `owner/repo`.
3. Detecção segura de arquivos do projeto, usando o perfil com `auto_detect`
   do ecossistema identificado.

A detecção inspeciona arquivos; não executa código do repositório para decidir
qual perfil usar. A seleção registra o nome, o motivo (`explicit`,
`repository_mapping` ou `detected`), as fases e o fingerprint do perfil.

Um nome desconhecido, a ausência de uma estratégia segura ou uma detecção
ambígua resulta em seleção `unsupported`, com o motivo da rejeição. Não há
fallback silencioso de um perfil explicitamente solicitado ou mapeado que seja
inválido para outro perfil. A fábrica não deve executar comandos do projeto
nesse estado.

O `profile_digest` persistido é o fingerprint SHA-256 da definição completa
do perfil validado. Ele é diferente do digest da imagem: identifica também
comandos, argumentos, fases e demais opções. Na retomada, a definição atual
precisa corresponder ao fingerprint salvo. Uma alteração ou remoção do perfil
interrompe sua autorização, evitando que um workflow retome com uma política
diferente da aprovada originalmente. Uma política revisada deve ser usada em
uma nova execução, não aplicada silenciosamente a uma execução existente.
