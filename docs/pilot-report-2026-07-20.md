# Relatório do piloto técnico — 20/07/2026

## Resultado inicial

O piloto executou 9 workflows reais (3 cenários, 3 rodadas, concorrência 1),
sem criação automática de pull requests.

| KPI | Resultado | Meta |
| --- | ---: | ---: |
| Conclusão | 77,8% (7/9) | >= 80% |
| First pass | 77,8% | >= 60% |
| Intervenção humana | 22,2% | acompanhar |
| Falha técnica | 0% | acompanhar |
| Custo médio | US$ 0,00314 | <= US$ 0,05 |
| Custo por conclusão | US$ 0,00260 | acompanhar |
| Latência p95 | 46,86 s | <= 120 s |

O gate falhou somente por uma conclusão: 7/9 ficou abaixo do mínimo de 80%.
As duas intervenções ocorreram no mesmo cenário de revisão de arquitetura.

## Diagnóstico e correções

1. Resultados de tarefas dependentes agora chegam ao executor com instrução
   explícita para reutilizar fatos e `evidence_ids` inline.
2. O judge não pode contradizer a validade estrutural de citações já confirmadas
   pelo validador determinístico; ele continua avaliando a suficiência da análise.
3. O planner ganhou uma segunda tentativa orientada quando produz dependências,
   ciclos ou `evidence_ids` estruturalmente inválidos, acumulando uso e custo.
4. Foram adicionados testes de regressão para citações em português e reparo do
   plano. A suíte passou com 72 testes, além de 2 testes opcionais ignorados.

## Confirmação pós-correção

Com o Docker reconstruído e dois workers registrados, 3 novas rodadas focadas em
arquitetura concluíram com sucesso. O gate focado passou com 100% de conclusão,
66,7% de first pass, nenhuma intervenção humana, nenhuma falha técnica, custo
médio de US$ 0,00313 e latência p95 de 28,12 segundos.

## Matriz final

| KPI | Resultado final | Meta | Status |
| --- | ---: | ---: | --- |
| Conclusão | 88,9% (8/9) | >= 80% | aprovado |
| First pass | 88,9% | >= 60% | aprovado |
| Intervenção humana | 11,1% | acompanhar | informativo |
| Falha técnica | 0% | acompanhar | aprovado |
| Custo total | US$ 0,02619 | — | informativo |
| Custo médio | US$ 0,00291 | <= US$ 0,05 | aprovado |
| Custo por conclusão | US$ 0,00271 | — | informativo |
| Latência p95 | 41,59 s | <= 120 s | aprovado |
| Timeouts | 0 | 0 | aprovado |

**Gate final: aprovado.** A única execução não concluída foi encaminhada ao
human-in-the-loop após três rejeições do judge em uma proposta de mitigação de
segurança. Isso foi uma escalada controlada, não uma falha técnica.

## Próximo critério de saída

O produto está liberado para um piloto controlado com design partner. A próxima
evidência necessária é comercial: ROI observado, satisfação do revisor humano,
taxa de aceitação das entregas e ausência de incidente em um repositório real.

Artefatos brutos (gerados localmente em `reports/`, fora do versionamento —
reproduzíveis com os comandos de benchmark em `docs/integrations.md` ou via o
workflow manual `benchmark.yml` do GitHub Actions, que publica os JSONs como
artifact `benchmark-reports`):

- `reports/pilot-internal.json`
- `reports/pilot-architecture-confirmation.json`
- `reports/pilot-architecture-final.json`
- `reports/pilot-final.json`
