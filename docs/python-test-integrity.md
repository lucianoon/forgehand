# Integridade estática de testes Python

O pipeline objetivo inclui `python_test_integrity` antes da aprovação de uma
entrega. Um comando de testes verde pode descobrir apenas a última classe ou
função quando duas definições usam o mesmo nome. A guarda recusa esses casos e
informa arquivo, nome e linhas para o executor corrigir sem eliminar cobertura.
O feedback usa o mecanismo existente de autocorreção; o judge relê o arquivo e
pode vetar a entrega mesmo com sandbox, pytest e avaliação subjetiva aprovados.

O escopo são os arquivos de teste Python realmente modificados nos diffs líquidos
produzidos pelo runtime: nomes `test*.py` ou `*_test.py`. Arquivos inalterados e
exclusões conhecidas ficam fora desta análise; exclusões continuam acionando os
validadores de comando normalmente. Um arquivo marcado como modificado que
desaparece antes da inspeção reprova a análise. Listas de comandos configuradas
por capability não desativam esta guarda.

A verificação usa somente AST, sem importar módulos nem executar código do
repositório no host. Inspeciona definições repetidas no mesmo bloco de um módulo
ou classe, incluindo métodos assíncronos. Nomes iguais em classes distintas,
overrides em subclasses, parametrização e stubs `typing.overload` reconhecidos
não representam duplicação. Ramos alternativos são inspecionados separadamente.

Esta é uma proteção específica contra sombreamento estático, não uma medição de
cobertura ou prova de descoberta de todos os testes. Não resolve alterações de
nomes por atribuição, imports, decorators arbitrários, criação dinâmica de
classes/funções, configurações próprias de descoberta nem interações entre
ramos condicionais. Testes e revisão humana continuam necessários.

A leitura exige POSIX, usa descritores relativos ao workspace configurado e
recusa links simbólicos, hardlinks e arquivos especiais. Sintaxe inválida,
fontes indisponíveis ou limites excedidos reprovam a análise. Limites: 100
arquivos, 128 KiB por arquivo, 2 MiB no total, 40 mil nós de AST por arquivo,
20 diagnósticos e cinco segundos entre inspeções. Não há fallback para executar
a fonte no host.
