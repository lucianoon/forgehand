## Why

Forgehand entrega mudanças em repositórios existentes, mas ainda exige que o usuário já tenha um projeto. O próximo valor é transformar uma ideia em uma primeira aplicação que ele possa abrir, experimentar e levar adiante.

## What Changes

- Adicionar um estúdio de produto: ideia, público, escopo e backlog revisáveis antes de aprovar a geração.
- Produzir uma aplicação web autocontida, com preview isolado e pacote de código para download, sem exigir GitHub.
- Persistir projetos e estado, separar autorização por proprietário/projeto, limitar chamadas e registrar consumo.
- Manter claro que esta versão é uma demonstração frontend, não backend, autenticação, deploy ou software aprovado para produção.

## Capabilities

### New Capabilities
- `idea-to-demo`: geração aprovada de aplicações demonstráveis a partir de ideias, preview isolado e exportação.

### Modified Capabilities

Nenhuma; o fluxo de manutenção e publicação por PR permanece inalterado.

## Impact

Novos modelos, serviço, armazenamento SQLite local, API autenticada e página do estúdio. Reutiliza o roteador de LLM existente; não executa código gerado no servidor nem cria recursos externos automaticamente.
