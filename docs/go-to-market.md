# Agent Forge — pacote de entrada no mercado

## Posicionamento

**Execução governada e auditável de mudanças de software.** O Agent Forge
transforma uma solicitação em plano, execução isolada, validação objetiva,
decisão humana e pull request, preservando evidências, custo e histórico.

Não competir como assistente individual de IDE. O comprador é o time de
plataforma, engenharia ou segurança que precisa controlar agentes trabalhando
em repositórios corporativos.

## Cliente ideal inicial

- software houses e times de produto com 20–200 desenvolvedores;
- empresas com backlog de manutenção e sistemas legados;
- fintechs, healthtechs e ambientes regulados;
- consultorias que precisam demonstrar rastreabilidade ao cliente.

## Piloto pago de seis semanas

1. Conectar um repositório não crítico e até três tipos de tarefa.
2. Executar baseline manual de tempo, custo e taxa de retrabalho.
3. Rodar no mínimo 30 workflows com aprovação humana obrigatória.
4. Medir conclusão, first-pass, custo, tempo, intervenção e aceitação do PR.
5. Entregar relatório de ROI, riscos e plano de expansão.

Antes do cliente, execute o piloto técnico interno reproduzível:

```bash
AGENT_FORGE_API_KEY=dev-key python -m app.evaluation.pilot \
  --base-url http://localhost:8001 \
  --rounds 3 \
  --output reports/pilot-internal.json \
  --fail-on-gate
```

Para confirmar apenas um cenário após uma correção, acrescente, por exemplo,
`--case-id architecture-review`. A opção pode ser repetida.

Esse ensaio não publica PRs automaticamente e mede nove workflows com teto
agregado explícito antes de qualquer gasto.

Hipótese comercial: taxa de implantação + mensalidade por capacidade, com
consumo de modelos transparente. Não cobrar por “número de agentes”.

## Demo de cinco minutos

1. Abrir `/dashboard` e mostrar saúde, workers e orçamento.
2. Enviar um ticket real com critérios de aceitação.
3. Acompanhar plano, tarefas, diffs, comandos e custo.
4. Demonstrar gate humano e trilha de auditoria.
5. Publicar a entrega como pull request.

## Métricas de compra

- taxa de workflows concluídos e first-pass;
- PRs aceitos sem alteração manual;
- tempo entre ticket e PR;
- custo por tarefa concluída;
- falhas bloqueadas pelo judge;
- percentual de execuções que exigem humano;
- incidentes de segurança e violações de política (meta: zero).

## Critérios para sair do piloto

- pelo menos 80% de conclusão no escopo acordado;
- zero alteração fora do workspace e zero segredo em log;
- 100% das mudanças publicadas com testes e auditoria;
- ROI positivo contra o baseline do cliente;
- runbook, responsável e procedimento de rollback definidos.
