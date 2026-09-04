# Linha de base dos evals

Cada arquivo `evals-<data>.md` aqui é a saída de
`uv run python -m app.evaluation.evals` em uma data, com o commit indicado,
copiada de `reports/evals-latest.md`. Publica-se a rodada inteira, inclusive
reprovações: a linha de base serve para comparar, não para exibir.

## 2026-09-04 — primeira rodada (parcial)

Duas execuções da suíte (`evals/cases.json`, orçamento US$ 1,50 cada) no
commit que introduz os evals. Nenhuma fechou o gate, e as causas ficaram
registradas:

| Execução | Resultado | Causa |
|---|---|---|
| 1 | 2/4 concluídos, US$ 0,95 | o carregador de `--env-file` do `uv` parou na primeira variável com espaços; validadores, `run_command` e referências web ficaram desligados. Ambiente, não produto. |
| 2 | 0/4 concluídos, US$ 0,58 | o caso `package-with-tests` reprovou `file_created` porque a evidência cumulativa da autocorreção marcava um arquivo criado na rodada 1 e editado na rodada 2 como `modified` (bug real, corrigido no mesmo PR com teste); os três casos seguintes falharam com HTTP 400 "credit balance is too low" da Anthropic. |

Próxima rodada válida: após recarregar os créditos do provedor, rodar

```bash
export ANTHROPIC_API_KEY=...
uv run python -m app.evaluation.evals --budget-usd 1.5 --gates evals/gates.json
cp reports/evals-latest.md evals/baseline/evals-$(date +%F).md
```

ou disparar o workflow `Evals` no GitHub com o segredo `ANTHROPIC_API_KEY`.
