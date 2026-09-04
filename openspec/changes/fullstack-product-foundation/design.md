## Context

O modelo declarativo do estúdio já limita entidades/campos e evita executar código de LLM. Vamos reutilizá-lo em um runtime versionado independente; não transformar automaticamente o grafo de repositórios nem consumir outra geração para exportar.

## Goals / Non-Goals

**Goals:** pacote executável com login, persistência, CRUD privado por usuário, concorrência otimista, exportação limitada, PostgreSQL, migração, operação documentada e testes do pacote real.

**Non-Goals:** SaaS irrestrito, compartilhamento entre equipes, pagamentos, autorização por papéis, SSO/MFA, recuperação de senha por e-mail, publicação pública ou alegar escala não medida. A agenda continua um cadastro, sem garantia de disponibilidade ou prevenção de conflitos de horário.

## Decisions

- Runtime FastAPI síncrono em threads, SQL parametrizado e psycopg_pool limitado para PostgreSQL; SQLite só no modo de desenvolvimento. Evita adicionar ORM ao Forgehand para quatro tabelas simples; os bancos compartilham SQL deliberadamente portátil. Migrações executadas por CLI separada, serializadas no PostgreSQL e versionadas, sem schema mutation no startup da API.
- Usuários provisionados por CLI interativa, sem senha padrão ou cadastro público. scrypt com salt aleatório, sessão opaca aleatória persistida apenas como hash, expiração e revogação. Cookies HttpOnly/SameSite=Strict; Secure obrigatório no modo de produção. Checagem de Origin em toda mutação e sem CORS. Limite de tentativas no banco, não apenas na memória de uma réplica.
- Todos os acessos a registros filtram user_id e entidade. IDs opacos; versão inteira obrigatória para edição/exclusão impede perda silenciosa de atualizações concorrentes. Paginação máxima 100, exportação máxima 1000 e corpos limitados. Modelo define campos aceitos; dados são validados no servidor e renderizados com textContent.
- Pacote contém runtime, modelo, briefing, CSS/JS, dependências fixadas, Dockerfile não root, Compose com volume PostgreSQL e README operacional. Nenhuma credencial ou estado de usuários é exportado. Um manifesto identifica versão do runtime e hash do modelo. Mantém demo sandbox sem login em paralelo.
- Interface para recepcionista: entrar, escolher cadastro, editar e localizar registros. Paleta reaproveita modelo (floresta #176c50, oceano #17647d, ameixa #765078), tinta #20312c, papel #f2f6f4 e branco #ffffff; Georgia no nome, system-ui em controles. Tela de trabalho prioriza formulário/lista; assinatura é a indicação explícita de usuário e dados persistentes, sem hero de marketing. Comparado com um novo dashboard genérico, manter o espaço de trabalho já conhecido reduz reaprendizado. Foco visível, estados vazios e mobile empilhado.

## Risks / Trade-offs

- [Base genérica não representa todas as regras de negócio] → explicitar limites e requisitos pendentes no pacote.
- [SQLite confundido com implantação escalável] → rejeitá-lo no modo de produção; PostgreSQL e sessões compartilhadas permitem múltiplos processos, mas exigem teste de carga real.
- [Sessões/cookies e força bruta] → validação de origem, hash forte, rate limit compartilhado e documentação de proxy confiável/HTTPS.
- [Mudança do modelo depois de haver dados] → congelar hash do modelo na migração e falhar readiness se divergir; alteração futura requer migração explícita.
- [Retenção de sessões/rate limit] → comando de limpeza operacional; não armazenar corpos ou credenciais em logs.

## Migration Plan

Exportar pacote sem iniciar serviços automaticamente. Operador configura .env e banco, executa migrate, cria usuário e sobe API; readiness verifica versão e hash. Atualizações futuras devem fazer backup e ensaiar restauração; rollback de aplicação apenas com schema compatível. A demo anterior e o estúdio continuam disponíveis.
