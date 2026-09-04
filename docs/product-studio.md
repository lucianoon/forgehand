# Estúdio de produto

`/studio` cria uma primeira versão navegável sem exigir um repositório existente.
É um fluxo separado da manutenção de código em `/dashboard`.

Além da demo de sessão, agora há **Baixar versão com login e banco**: pacote
independente com backend, autenticação, dados privados persistentes e PostgreSQL.
Esse download não transforma a prévia em aplicação de produção nem faz outra
chamada de IA. Veja a [base full-stack e suas verificações](fullstack-product-foundation.md).

## Executar localmente

Com as dependências instaladas (`uv sync --extra dev --locked`) e a chave do
provedor no arquivo ignorado `.env.local` (ver [OpenAI direto](openai.md)):

```bash
PRODUCT_STUDIO_ENABLED=true LLM_PROVIDER_BACKEND=openai \
  uv run uvicorn app.main:app --env-file .env.demo --env-file .env.local \
  --host 127.0.0.1 --port 8000
```

Abra `http://127.0.0.1:8000/studio`. No perfil `.env.demo`, a chave **do Forgehand**
é `dev-key`; nunca cole a chave OpenAI na página. Variáveis já exportadas no shell
têm precedência sobre arquivos de ambiente. Não exponha essa chave de demonstração
em um ambiente compartilhado. Para outro provedor, configure o backend correspondente.

O histórico do estúdio fica em `data/product-studio.sqlite3`, configurável por
`PRODUCT_STUDIO_DATABASE`. Preserve esse arquivo em volume persistente e faça backup.
O perfil demo mantém os workflows antigos em memória, mas o estúdio usa SQLite.
Esta implantação é single-host, não uma fila distribuída.

## Jornada

1. Descreva ideia, público, projeto autorizado e orçamento estimado (US$ 0,01–5).
2. Revise nome, resultado, funcionalidades, backlog, critérios de aceite e limites.
3. Aprove explicitamente para iniciar a segunda chamada de IA.
4. Experimente a aplicação, valide o checklist e baixe o ZIP.

O motor suporta até quatro entidades, oito campos por entidade e seis exemplos
iniciais. Campos: texto, número, data, horário e opções. Oferece criação/edição por
formulário, exclusão confirmada, cartões, busca textual e exportação JSON de todos
os registros. Não oferece tabela, filtros avançados ou exportação CSV.

O ZIP contém `index.html` autocontido, `model.json`, `brief.json` e `README.md`.
Abra `index.html` diretamente no navegador, sem instalar dependências. O código e
o modelo usados ficam embutidos nele; `model.json` é uma cópia para referência.
Editar somente essa cópia não atualiza a aplicação.

## Limites e segurança

- A IA produz dados estruturados; JavaScript e CSS vêm de um renderer versionado.
  Conteúdo do modelo não vira HTML executável. A prévia usa iframe de origem opaca,
  sem acesso à página principal, rede, popups ou downloads internos. Exporte os
  dados pela aplicação baixada, não pelo iframe.
- Os **registros da demo** duram somente enquanto a página estiver aberta. Não há
  backend, login real, sincronização, importação, pagamentos ou banco compartilhado.
  O banco do estúdio preserva o projeto gerado, não os registros inseridos na prévia.
- O briefing pode prometer algo além do motor: revise e ajuste antes de aprovar.
  `ready_for_preview` significa disponível para experimentar, não certificado ou
  aprovado para produção. O checklist manual não muda esse status.
- Chave de criação idempotente e aprovação única evitam repetir gerações por cliques
  duplicados. Se a conexão cair, use Atualizar ou recupere pelo histórico.
- Operações interrompidas expiram após cinco minutos; não são repetidas automaticamente.
  Falhas sem medição preservam a reserva de custo desconhecido. Para tentar novamente,
  crie uma nova ideia conscientemente, com outra chave de idempotência.
- Reservas conservadoras incluem estimativa de entrada, saída máxima e tentativas
  do provedor. Não são garantia de teto de cobrança; configure limites na conta do
  provedor. Não há publicação, criação de repositório ou merge automático.
- APIs exigem `X-API-Key`: criação requer `operator`, aprovação requer `approver`.
  Consulta, prévia e download verificam proprietário (`client_id`) e projeto.

## API

Para evoluir um produto aprovado em um repositório existente, use o
[plano de entregas incrementais](incremental-product-delivery.md): decisões e
contexto persistentes, orçamento por tentativa e próxima entrega somente depois
de merge verificado. Salvar o plano não inicia IA.

| Operação | Endpoint |
|---|---|
| Criar briefing | `POST /products` |
| Histórico | `GET /products?project_id=...` |
| Consultar | `GET /products/{id}` |
| Aprovar briefing editado | `POST /products/{id}/approve` |
| Documento para iframe (JSON, não HTML ativo) | `GET /products/{id}/preview` |
| Código ZIP | `GET /products/{id}/download` |
| Pacote full-stack com login/banco | `GET /products/{id}/fullstack` |

Contratos tipados disponíveis em `/docs`. O estúdio desativado responde 503;
clientes sem acesso recebem 404 nos IDs de outros proprietários.

## Validação observada — 03/09/2026

Piloto real com OpenAI: ideia de agenda de barbearia → briefing editado → aprovação
→ modelo da aplicação → prévia → ZIP. Duas gerações, 1.780 tokens, custo estimado
reportado de US$ 0,001576; não é uma fatura nem benchmark generalizável.

No Chrome foram verificados cadastro de um agendamento fictício, busca, edição,
exclusão confirmada e download do ZIP. O ZIP foi aberto separadamente e exportou
os quatro registros iniciais em `dados.json`. Histórico recuperado em outra página
e após reinício do servidor, sem repetir chamadas de IA. Tela de 390 × 844 px
verificada sem rolagem horizontal na página principal.

A revisão encontrou e corrigiu duas falhas: o navegador integrado não renderizava
o documento criado em iframe inicialmente oculto (corrigido montando um iframe
novo quando o painel já está visível); o sandbox bloqueava submissão nativa de
formulário (corrigido com botão de gravação local e validação HTML explícita).
A prévia passou a renderizar no navegador integrado, sem liberar `allow-same-origin`
ou `allow-forms`. O teste `tests/web/product-demo.test.cjs` falhou antes da correção
da gravação e passou depois, cobrindo CRUD e busca sem submissão nativa.

Seis cenários Python cobrem contratos, aprovação/replay, concorrência, propriedade,
reinício, expiração, custo, falhas e isolamento. A suíte padrão local passou com
531 testes e 21 skips (integrações opt-in não executadas nesta rodada); cinco
testes JavaScript passaram. Ruff e mypy também foram verificados. Esses testes
validam o motor; os requisitos semânticos de cada ideia ainda exigem revisão humana.
