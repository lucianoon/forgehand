# Aceite do piloto supervisionado

Este registro separa **evidência disponível** dos critérios para liberar uma
instalação concreta. O escopo é uma equipe autorizada, repositórios selecionados,
mudanças entregues por PR e revisão/merge humanos. Não qualifica SaaS entre
empresas, autonomia de merge, alta disponibilidade ou um SLA público.

## Evidência disponível em 6 de setembro de 2026

| Evidência | Resultado observado | Limite |
| --- | --- | --- |
| Matriz real em `42c905f` | 5/5 cenários; PRs e CI verdes; verificação independente; 4/5 na primeira tentativa | Dois repositórios pequenos de fixture, Docker Desktop local, mesma família de modelo; não mede diversidade de clientes |
| Medição da mesma matriz | 59.546 tokens; US$ 0,0270428; média 44,43 s e p95 53,46 s entre cinco casos | Custos estimados pelo aplicativo; p95 de workflows dessa amostra, não latência HTTP ou SLO |
| Reinício da API e idempotência em `42c905f` | Mesmo workflow recuperado; estado/custo preservados; reenvio não duplicou o job | Ensaio local; não é recuperação completa de perda de host |
| Correção de sobrescrita, [PR #37](https://github.com/lucianoon/forgehand/pull/37), merge `48b7207` | `create` moderno rejeita conteúdo diferente em arquivo existente; CI: 1.339 Python, 14 JavaScript, 17 sandbox, 7 instalação | Não proíbe uma remoção explícita por `replace`/`delete`; contrato legado `files` mantém compatibilidade |
| CI de instalação em Linux | Build, dois workers, crash/recovery e backup/restore determinísticos | Não prova operação no futuro host, acesso real ao GitHub/LLM ou capacidade sustentada |

PRs da matriz: [Python #10](https://github.com/lucianoon/forgehand-fixture-python/pull/10),
[Node #4](https://github.com/lucianoon/forgehand-fixture-node/pull/4),
[Python #11](https://github.com/lucianoon/forgehand-fixture-python/pull/11),
[Node #5](https://github.com/lucianoon/forgehand-fixture-node/pull/5) e
[Python #12](https://github.com/lucianoon/forgehand-fixture-python/pull/12).
Um ensaio direcionado anterior, [Python #9](https://github.com/lucianoon/forgehand-fixture-python/pull/9),
ficou verde automaticamente, mas removeu um teste existente. Essa revisão originou
o PR #37; esse resultado não deve ser contado como aceite humano sem ressalvas.
A matriz acima foi feita **antes** do merge #37; não constitui reteste de `48b7207`.

## Registro da instalação a liberar

Preencha o registro abaixo e mantenha os anexos em armazenamento privado da equipe,
sem credenciais ou fontes privadas publicadas no repositório deste produto:

| Campo | Estado inicial |
| --- | --- |
| Equipe, operador e substituto | A definir |
| Host Linux dedicado, acesso administrativo e domínio TLS | A definir; nenhuma implantação de produção comprovada neste registro |
| Repositórios autorizados e respectivos revisores | A definir |
| Revisão completa, digest da imagem runtime e PostgreSQL | A registrar do artefato efetivamente instalado |
| Projeto Compose, data root, workers, fingerprint e perfis | A registrar; padrão operacional: um host, dois workers |
| Orçamento por workflow e teto da campanha | A aprovar antes de chamadas; não inferir dos custos das fixtures |
| RPO/RTO, retenção e janela de backup | A aprovar; referência inicial: 24 h / 60 min |
| Relatório de aceite, incidentes conhecidos e data de revisão | A preencher |

## Portões de liberação

Marque concluído apenas com evidência da **mesma revisão/artefato** que será usado.
Este checklist permanece aberto até o responsável anexar o resultado; código ou
configuração implementados não são evidência de execução no host do piloto.

- [ ] Correção #37 incluída no artefato e regressão de sobrescrita verificada.
- [ ] Build padrão `runtime` com dependências travadas, CI verde e imagem imutável
      qualificados; imagem anterior preservada para rollback.
- [ ] Linux dedicado, socket restrito, dados persistentes no mesmo caminho absoluto,
      configuração privada fora do checkout e PostgreSQL sem porta pública.
- [ ] TLS testado de um cliente autorizado; credenciais e acesso restritos aos
      repositórios/projetos selecionados; métricas e diagnóstico privados.
- [ ] `/readyz` e `forgehand doctor` confirmam revisão/fingerprint e dois workers
      compatíveis; nenhum job incompatível, legado sem vínculo ou sem configuração.
- [ ] Entrega canário real na revisão instalada: PR, SHA, CI, verificação independente,
      custo conhecido e revisão humana do diff incluindo preservação dos testes.
- [ ] Reinício, idempotência, queda de worker, indisponibilidade do banco e retomada
      ensaiados em instalação isolada equivalente, sem perda ou execução duplicada.
- [ ] Backup frio copiado para fora do host e restauração completa ensaiada com RPO/RTO
      medidos; nenhuma aprovação humana concedida pela recuperação.
- [ ] Rollback ensaiado com decisão de compatibilidade de schema/checkpoints, dados
      preservados e reconciliação dos efeitos no GitHub; não apenas troca de tag.
- [ ] Alertas recebidos pelo operador e substituto; disco, readiness, fila e validade
      dos backups observados; capacidade medida no limite de concorrência escolhido.
- [ ] Campanha abaixo concluída com falhas incluídas no denominador e decisão dos
      revisores registrada. Limitações comunicadas à equipe participante.

## Campanha em repositórios existentes

Ponto de partida proposto: **20 ordens em pelo menos três repositórios autorizados**,
divididas entre correção de defeito, testes, pequena funcionalidade, refatoração e
configuração/documentação. Isso é um portão de piloto, não tamanho de amostra que
comprove confiabilidade universal. Se houver menos repositórios, declare o escopo
menor do aceite. Escolha bases por SHA e casos com comportamento verificável antes
de iniciar; mantenha também os casos que falharem.

Execute primeiro de forma sequencial e depois com duas admissões concorrentes,
respeitando limites por workflow e o teto financeiro total acordado. Não recicle
silenciosamente casos falhos até obter uma amostra toda verde. Uma repetição é nova
tentativa vinculada ao caso original e continua contando em custo e intervenção.

Para cada ordem, registre revisão/digest do harness, repositório/base SHA, perfil,
modelo/configuração, ID/idempotência, critérios independentes, duração, tokens,
custo conhecido/desconhecido, tentativas, PR/head SHA, CI e avaliação humana.
Registre também alterações manuais, testes removidos/enfraquecidos, arquivos fora
de escopo, efeitos repetidos e motivo de rejeição. Publicar PR não equivale a
aceite; um teste verde não substitui a leitura do diff.

| Critério proposto para o primeiro piloto | Medição |
| --- | --- |
| Pelo menos 16/20 entregas aceitas pelos revisores sem reparo manual | Aceites sobre todas as ordens admitidas; separadamente, taxa com intervenção |
| Zero perda de trabalho, efeito externo duplicado ou violação de isolamento | Registro dos ensaios e revisão dos incidentes; qualquer ocorrência bloqueia ampliação |
| Zero remoção não aprovada de testes existentes | Comparar base e head além de verificar CI; registrar exceções explicitamente autorizadas |
| Custo conhecido em todas as ordens; total dentro do teto aprovado | Medição do aplicativo reconciliada com uso do provedor quando disponível |
| Capacidade e latência aceitáveis para a equipe com dois workers | Tempo na fila e tempo total por classe; publicar amostra e percentis, sem extrapolar SLA |
| Recuperação dentro dos RPO/RTO acordados | Medição ponta a ponta conforme o runbook, incluindo canário e reconciliação |

Revisores e operador aprovam o piloto com nome/data e limites de uso. Se um portão
falhar, mantenha a instalação restrita à qualificação e registre o próximo ensaio.
Reavalie após mudança de modelo, perfil, dependências, imagem, configuração ou classe
de repositório: sucesso numa combinação não qualifica automaticamente outra.

Procedimentos: [instalação](team-installation.md),
[operação e rollback](production-runbook.md), [backup](team-backup.md),
[entrega pela CLI](developer-delivery-cli.md).
