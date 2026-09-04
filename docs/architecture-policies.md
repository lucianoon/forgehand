# Políticas de arquitetura executáveis

O Forgehand pode bloquear uma entrega Python quando ela viola limites de imports
aprovados pelo operador. O verificador usa AST e não importa nem executa os arquivos
analisados. É um controle de arquitetura, não um substituto do sandbox ou uma prova
de todas as dependências possíveis em runtime.

## Configuração

Acrescente o campo `architecture` a um perfil Python já aprovado em
`FACTORY_BUILD_PROFILES_JSON`, mantendo imagem fixada por digest e fases existentes:

```json
{
  "architecture": {
    "version": 1,
    "source_roots": ["."],
    "rules": [
      {
        "id": "domain-isolation",
        "source": "app.domain",
        "forbidden": ["app.infrastructure", "app.api", "requests"],
        "remediation": "Dependa de interfaces do domínio e injete os adaptadores pela camada de composição."
      }
    ]
  }
}
```

Esse fragmento não é um perfil completo nem uma política universal para todos os
repositórios. O operador escolhe os limites adequados ao projeto. O agente não pode
alterar a política modificando arquivos no checkout: ela vem da configuração do
servidor, não de um arquivo produzido pelo modelo.

`source_roots` define as **raízes de importação**: com `.` o arquivo
`app/domain/service.py` é `app.domain.service`; com `src`,
`src/app/domain/service.py` também é `app.domain.service`. Raízes devem ser relativas,
normalizadas, existentes e não sobrepostas. A origem de cada regra precisa corresponder
a pelo menos um módulo; uma regra não exercitada bloqueia em vez de passar em silêncio.

A comparação respeita segmentos: proibir `app.api` também proíbe `app.api.routes`,
mas não `app.api_client`. Imports absolutos, relativos, aliases, imports em funções
e dentro de `TYPE_CHECKING` são verificados. `from app import infrastructure` é
tratado conservadoramente como dependência de `app.infrastructure`; em Python o nome
pode ser atributo, então esse caso pode exigir revisão humana de um falso positivo.

## Onde ocorre o bloqueio

1. A seleção fixa o fingerprint do perfil e o digest da política. Alterar regras
   ou remover a identidade esperada invalida a seleção antiga.
2. Planner e executor recebem as regras aprovadas como orientação antes de atuar.
3. O runner inspeciona os fontes antes dos comandos de build. Uma falha impede essas
   fases; o relatório é persistido na tentativa.
4. Depois de todas as fases bem-sucedidas, o runner inspeciona novamente, para
   detectar imports introduzidos por geração ou preparação de código.
5. O judge recebe um veto objetivo e o agente recebe regra, arquivo, linha,
   dependência e orientação de correção. As tentativas continuam sujeitas aos
   limites de custo, iterações e aprovação humana existentes.
6. Antes da publicação, o Forgehand exige evidência completa e aprovada com o digest
   esperado. Um resultado agregado `success` não contorna evidência ausente,
   incompleta, reprovada ou correspondente a outra política.

Os detalhes ficam em `TaskAttempt.build_validation.architecture`, e também no
relatório de build anexado ao resultado. O feedback usa nomes
`architecture:<regra>:<código>`; o resumo de revisão inclui as primeiras dez
ocorrências. Não há merge automático nem permissão para o agente afrouxar regras.

## Limites e casos recusados

- Primeira versão: Python. Perfis Node com `architecture` são rejeitados.
- Até oito raízes, 30 regras, 20 prefixos proibidos por regra e 16 KB por política.
- Por inspeção: até 1.000 arquivos Python, 10.000 entradas de diretório, 128 KiB por
  arquivo, 8 MiB de fontes, profundidade 24 e prazo cooperativo de cinco segundos.
  Um arquivo em leitura ou parse pode terminar antes de observar o prazo; não é um
  timeout de processo nem garantia de isolamento de CPU.
- Até 50 diagnósticos. Exceder limites produz análise incompleta, nunca aprovação.
- Leitura por descritores relativos sem seguir links. Links simbólicos e arquivos
  especiais na árvore analisada são recusados; arquivos Python com hardlinks também.
  `.git`, `.venv`, `venv`, `node_modules` e `__pycache__` são ignorados quando são
  diretórios reais. Não coloque módulos governados nesses diretórios excluídos.
- Erros de sintaxe, fontes inacessíveis e raízes vazias/não exercitadas bloqueiam.
  O relatório não inclui trechos de fonte nem mensagens brutas de erro do sistema.
- Imports wildcard e chamadas reconhecidas de `__import__`/`importlib.import_module`
  em módulos governados são recusados, inclusive aliases diretos de `importlib`.
  A análise não resolve aliases arbitrários, reexports transitivos, `sys.path`,
  reflexão, plugins, código compilado ou imports construídos por outros mecanismos.

O resultado é uma fotografia das fontes inspecionadas. Não comprova imutabilidade
contra alterações externas posteriores. O isolamento/ownership do workspace e os
demais controles de publicação continuam necessários.

## Ativação e compatibilidade

Perfis sem `architecture` mantêm comportamento e fingerprints anteriores. Nenhuma
política é ativada implicitamente em repositórios existentes. Configure o perfil,
atualize API/workers para a mesma versão e reinicie os serviços em uma janela
apropriada. Uma seleção em andamento com política diferente deve ser revisada;
não altere o checkpoint para contornar o bloqueio.

Esta implementação foi validada com fontes locais, transporte Docker simulado,
grafo completo com executores simulados e testes de publicação. Não executou
modelos pagos, não publicou PRs nem reiniciou serviços. A validação demonstra o
controle e seu ciclo de correção, não a qualidade arquitetural universal de uma
aplicação gerada.
