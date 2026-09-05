# Contrato das métricas de avaliação

As métricas do benchmark e dos evals medem o resultado observado pelo runner.
Conclusão do workflow não equivale a aceitação independente da entrega; a
qualificação da fábrica executa verificadores adicionais sobre o SHA publicado.

## Definições

- `completion_rate`: casos com `completed=true` / casos da rodada.
- `first_pass_rate`: casos concluídos cujas tarefas tiveram exatamente uma
  tentativa / casos da rodada. Falha, cancelamento e espera por decisão humana
  nunca contam como first pass, mesmo com uma tentativa por tarefa. Uma lista
  vazia de tarefas também não fornece evidência de first pass.
- `total_cost_usd`: soma do custo registrado de todos os casos.
- `cost_per_completed_usd`: custo total da rodada / conclusões. Inclui falhas;
  retorna `null` quando não há conclusões. Consumidores devem exibir "não
  aplicável", sem converter esse valor para zero.
- `quality_gate.checks.has_cases`: impede aprovação sem casos avaliados.

Exemplo: uma entrega de US$ 0,20 e uma falha de US$ 0,80 representam US$ 1,00
por entrega concluída, não US$ 0,20. First pass inconsistente em resultados
importados é ignorado na taxa agregada se `completed=false`.

## Limites e comparação histórica

Os custos são os valores disponíveis no metering da aplicação. Chamadas sem
usage, polling interrompido e timeouts podem deixar custos incompletos. Estes
indicadores não são conciliação da fatura nem garantia de teto de cobrança.

Relatórios antigos não são reescritos. Ao comparar rodadas anteriores à correção,
recalcule as taxas e o custo por conclusão a partir dos resultados individuais,
ou marque a mudança de definição. Estados `completed` e `ready_for_human_review`
mantêm a definição de conclusão já usada pelo runner; não comprovam merge,
produção ou ausência de intervenção em toda a história do workflow.

Verificação local sem chamadas pagas:

```bash
uv run pytest tests/unit/test_benchmark.py tests/unit/test_evals.py tests/unit/test_pilot.py -q
```
