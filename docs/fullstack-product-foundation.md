# Base full-stack do estúdio

O estúdio agora exporta uma aplicação independente com backend e persistência,
além da demo frontend anterior. Use **Baixar versão com login e banco**, ou
`GET /products/{id}/fullstack` autenticado com a chave Forgehand. O produto precisa
estar pronto e pertencer ao cliente/projeto autorizado. Exportar não faz nova
chamada de IA nem inclui a chave OpenAI.

## O que é entregue

- FastAPI, interface de login e CRUD persistente, busca paginada e exportação JSON.
- Contas provisionadas por CLI, senhas scrypt, sessões revogáveis de oito horas e
  dados privados por usuário. O hash da sessão fica no banco; cookies são HttpOnly,
  SameSite=Strict e Secure no modo de produção.
- Validação de campos no backend; proteção de origem; limite compartilhado de
  tentativas de login; edição/exclusão com versão para detectar concorrência.
- PostgreSQL com pool limitado para implantação e SQLite só em desenvolvimento.
- Migração inicial explícita/versionada, verificação do hash do modelo, health e
  readiness, imagem não root, Compose e instruções de operação/backup/restauração.
- Manifesto de versão/hashes, código completo e briefing original, sem dados de
  contas ou exemplos da demo. O runtime não importa Forgehand nem depende de IA.

O README dentro do ZIP contém os comandos executáveis e as configurações. A
interface mantém o espaço de trabalho do produto, com identificação da conta e
estado de persistência, sem transformar a demo em uma alegação de produção.

## Verificações observadas — 03/09/2026

- Suíte local: **540 testes Python aprovados, 21 skips** de integrações opt-in não
  relacionadas; cinco testes JavaScript aprovados, Ruff limpo e mypy limpo em 69
  arquivos. Os nove cenários novos incluem SQLite e PostgreSQL reais.
- Login, cadastro, sessão/registro recuperados após reinício, edição, busca,
  exclusão confirmada e arquivo JSON exportado
  conferidos no navegador. Layout de 390 px: sem overflow horizontal e uma coluna
  de trabalho. A revisão visual manteve os controles e o estado de conta explícitos.
- Exportação do ZIP validada: manifesto corresponde aos bytes, nenhuma credencial
  do estúdio é incluída e migração/startup funcionam em processo separado.
- Build Docker concluído a partir do pacote, com dependências instaladas do zero.
  Compose validado. Aplicação em container não root/read-only com dois workers e
  PostgreSQL 16 respondeu readiness e às operações autenticadas.
- Ensaio local pequeno: 200 requisições (20 criações, 180 leituras), concorrência
  dez, dois workers, zero erros; mediana **44,24 ms**, p95 **53,16 ms**, duração
  **0,902 s**. As 20 gravações foram conferidas na exportação e excluídas depois.
  Esse número exclui login, usa base pequena e computador local: **não é prova de
  capacidade em produção nem benchmark independente**.
- Backup completo via pg_dump restaurado com pg_restore em banco separado, sem
  erros; verificação de modelo/schema e conta restaurada confirmadas.

## O que ainda falta para produção

Esta entrega é uma base reutilizável de aplicações CRUD, não um gerador universal
de sistemas robustos. A agenda ainda não resolve disponibilidade de profissionais,
conflitos de horário, equipes compartilhadas ou permissões por papel. Também não
inclui SSO/MFA, e-mails, pagamentos, fila de tarefas ou publicação pública.

Antes de produção: requisitos reais e regras do negócio; revisão de segurança;
TLS/proxy confiável e ajustes de rate limit; lock completo e imagens por digest;
observabilidade e alertas; backups agendados com ensaio periódico; testes de carga
representativos, migrações evolutivas e deploy/rollback em staging. O modelo fica
congelado após a migração inicial; mudar seus campos exige uma migração explícita.

Referência adotada para o custo do hash: [OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
