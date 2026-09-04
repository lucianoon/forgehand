## Why

O estúdio entrega demos de sessão, mas o usuário quer sistemas persistentes e operáveis. Precisamos de uma base full-stack reutilizável e verificada antes de prometer escala ou produção.

## What Changes

- Exportar um pacote full-stack independente a partir de um modelo já aprovado, preservando o download frontend atual.
- Incluir backend CRUD, autenticação por sessão, dados privados por usuário, validação e controle de edição concorrente.
- Disponibilizar PostgreSQL com pool limitado, SQLite de desenvolvimento, migração inicial versionada, health/readiness, Docker Compose e instruções de backup/restauração.
- Integrar o novo download ao estúdio com limites explícitos. O runtime gerado não depende de IA, chave OpenAI ou do Forgehand.
- Verificar isolamento, persistência, revogação, conflitos, PostgreSQL e jornada visual; registrar resultados sem alegar certificação de produção.

## Capabilities

### New Capabilities
- `fullstack-product-package`: aplicação independente autenticada e persistente derivada do modelo de produto aprovado.

### Modified Capabilities

Nenhuma especificação principal existente é alterada; extensão aditiva ao estúdio em desenvolvimento.

## Impact

Novos assets versionados de runtime em app/product_runtime, exportador, endpoint de pacote e botão no estúdio; testes e documentação. PostgreSQL usa o extra existente psycopg, sem mudar o grafo de manutenção de repositórios. Sem deploy público, novos repositórios, merge ou alteração de credenciais.
