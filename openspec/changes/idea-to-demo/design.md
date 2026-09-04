## Context

O usuário quer uma primeira versão demonstrável, não outra rodada de fixtures. O backend existente já autentica clientes e centraliza chamadas LLM, mas seu grafo assume um repositório de origem.

## Goals / Non-Goals

**Goals:** ideia → escopo editável/backlog → aprovação → demo HTML/CSS/JS navegável → ZIP com código, critérios e instruções. Histórico local durável e acesso por proprietário/projeto.

**Non-Goals:** backend gerado, login real, pagamentos, publicação pública, criação de repositórios e garantia automática de correção funcional.

## Decisions

- Serviço separado do grafo Git: evita inventar repositório ou anunciar PR onde o produto é um pacote local. Reutiliza ProviderRouter e autenticação.
- SQLite em data/ para histórico single-host e transições condicionais. Uma chave de idempotência de criação e aprovação única impedem cliques duplicados de iniciar gerações pagas. Interrupções ficam registradas, sem retry automático.
- Dois pedidos estruturados: briefing e modelo declarativo de aplicação CRUD (entidades, campos, opções e registros de exemplo). O usuário pode editar o briefing enviado na aprovação. Limites de saída, tempo e reserva estimada conservadora antecedem chamadas; falhas sem metering retêm reserva desconhecida.
- Um renderer versionado materializa o modelo em HTML/CSS/JS autocontido. Texto do modelo só entra como dados escapados, nunca código ou HTML executável. Preview srcdoc em iframe sandbox allow-scripts sem same-origin, CSP de rede negada e formulários tratados pelo renderer. ZIP com nomes fixos contém index.html, modelo, briefing e instruções. Cadastro, edição, exclusão, busca e exportação de dados são comportamentos reais do renderer; não é um gerador irrestrito de aplicações.
- Interface independente /studio ligada ao painel, mesma paleta verde/papel (#17211b, #647069, #f4f3ec, #fffef8, #147d4b), Georgia para títulos e system-ui para controles. Sua assinatura é a bancada de três etapas reais: descrever, aprovar, experimentar; preview amplo ao lado do briefing. Layout empilhado no mobile, foco visível, sem animação decorativa.

## Risks / Trade-offs

- [HTML gerado malicioso] → iframe opaco, CSP restritiva, sem formulários, popups, navegação superior ou credenciais; download identificado como código não revisado.
- [Demo não satisfaz todos os requisitos] → checklist manual do usuário e estado ready_for_preview, nunca alegar aprovação funcional automática.
- [Reinício no meio de geração] → expiração de operação sem retry pago automático; acesso durável ao estado.
- [Orçamento estimado] → checagem conservadora por chamada, reservas de falhas e limites explícitos; não substituir controles de cobrança do provedor.
- [SQLite local] → piloto single-host, não arquitetura distribuída; produto desativado por configuração por padrão.

## Migration Plan

Adicionar feature flag e banco separado em data/, habilitar somente no piloto; rollback desativa o estúdio preservando artefatos e workflows existentes.
