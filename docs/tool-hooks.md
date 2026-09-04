# Hooks de ferramentas

O Forgehand aceita políticas declarativas do operador nos eventos `pre_tool`,
`post_tool` e `tool_error`. Elas rodam no ToolLoop de planner, executor (também
escalado) e judge, incluindo agentes vinculados às leases da fábrica.

## Configurar

Defina a mesma configuração na API e em todos os workers, depois reinicie esses
processos. Nenhum arquivo de política é carregado do repositório gerado e o modelo
não pode modificar as regras. Por padrão `TOOL_HOOKS_JSON=[]` desliga os hooks.

Exemplo de ambiente (não é necessário criar outra chave de API):

```dotenv
TOOL_HOOKS_JSON='[{"id":"audit-calls","event":"pre_tool","action":"audit"},{"id":"block-checks","event":"pre_tool","tool":"run_check","agent":"*_executor","action":"deny"},{"id":"limit-read-results","event":"post_tool","tool":"read_file","action":"suppress","output_exceeds_chars":8000}]'
TOOL_HOOKS_TIMEOUT_SECONDS=2
AUDIT_LOG_BACKEND=jsonl
AUDIT_LOG_PATH=./data/audit.jsonl
```

Esse exemplo registra chamadas, impede `run_check` nos executores e oculta
resultados de `read_file` com mais de 8.000 caracteres. **Não bloqueia o pipeline
de validação objetiva**, que não passa pelo ToolLoop. Remova `block-checks` se
quiser que o executor possa explorar executando seus validadores configurados.

| Campo | Comportamento |
|---|---|
| `id` | Único; 1–64 letras ASCII, números, `_` ou `-` |
| `event` | `pre_tool`, `post_tool` ou `tool_error` |
| `action` | `audit` (padrão), `deny` somente no pre, `suppress` somente no post |
| `tool` | Glob sensível a maiúsculas; padrão `*` |
| `agent` | Glob sensível a maiúsculas; padrão `*` |
| `output_exceeds_chars` | Opcional, somente post; aplica se o resultado exceder o valor |

Os agentes usam nomes `planner`, `judge`, `backend_executor`, `quality_executor`,
`docs_executor` e `architecture_executor`. Escalados conservam o nome do perfil.
As ferramentas atuais são `read_file`, `list_directory`, `search_repository`,
`run_check`, `run_command` e `fetch_url` (as três quando habilitadas). Não há matcher por conteúdo de argumentos ou por
paths; `tool` identifica a ferramenta, não o arquivo acessado.

Configuração inválida impede a inicialização: campos desconhecidos, eventos e
ações incompatíveis, IDs duplicados, mais de 64 regras ou mais de 65.536 caracteres.
Não são aceitos comandos shell, URLs de execução, imports ou scripts.

## Execução e falhas

- Todas as regras correspondentes são consideradas em ordem; `audit` não desfaz
  um `deny` ou `suppress`. Nenhuma regra concede permissões adicionais.
- `pre_tool` negado: a ferramenta não roda; o modelo recebe erro de política.
  Não se emite post/error para uma chamada que não foi executada.
- `post_tool` suprimido: a ferramenta já rodou, mas o resultado original não é
  encaminhado ao modelo nem ao preview do trace. **Não há rollback.**
- Exceção da ferramenta emite `tool_error`, não `post_tool`. Cancelamento continua
  sendo cancelamento, nunca um sucesso artificial.
- Auditoria falha ou excede o timeout: o loop falha com mensagem segura. Antes da
  ferramenta, impede execução; depois, impede o envio do resultado, sem desfazer
  ações. Não há fallback silencioso para execução sem hooks.
- O teto de chamadas é aplicado a cada item do lote. Chamadas negadas ou
  desconhecidas consomem slots; chamadas excedentes não são executadas. Se o
  provider ignorar a solicitação final e pedir mais ferramentas, o loop termina.

## Auditoria

Com pelo menos uma regra, cada evento do ciclo de chamada é registrado, mesmo sem
matcher correspondente, no backend de auditoria configurado. A rota administrativa
de auditoria existente permite consultar os registros. Eventos têm ações
`tool.pre_tool`, `tool.post_tool` e `tool.tool_error`, desfecho e IDs de
workflow/projeto/cliente obtidos do job autenticado, não dos argumentos do modelo.

`detail` contém somente ID gerado da execução do loop, ordinal da chamada, nome
conhecido da ferramenta (`<unknown>` para nomes não registrados), agente, ID de
tarefa e IDs das regras correspondentes. Não inclui prompts, argumentos, conteúdo
dos arquivos, resultados ou IDs de chamada fornecidos pelo modelo. Execuções
diretas fora do serviço têm escopo de workflow nulo. JSONL continua sendo uma
opção single-node, não uma trilha imutável para múltiplos servidores.

## Limites desta etapa

Estes são hooks de **exploração dos agentes**, não de toda a fábrica. Grounding
inicial, aplicação dos arquivos, build, validadores objetivos e entrega Git/PR
mantêm seus controles próprios; não são interceptados por estas regras.
Suprimir uma leitura não garante confidencialidade se o mesmo conteúdo também
entrar pelo grounding ou por outro canal. O trace preexistente de ferramentas
permitidas continua podendo conter argumentos e previews; ele é diferente dos
registros sanitizados de hooks.

Não implementa plugins executáveis, MCP, carregamento de skills, hooks de sessão,
compactação de contexto ou edição dinâmica de políticas. Não substitui sandbox,
restrições de paths/comandos, orçamento nem aprovação humana. Webhooks HTTP de
workflow continuam sendo um mecanismo separado de notificação.

## Verificação

```bash
uv run pytest -q tests/unit/test_tool_hooks.py tests/unit/test_tool_use.py
```

Os testes usam providers simulados; não fazem chamadas pagas. Cobrem bloqueio real,
supressão, erros/timeouts de auditoria, limites locais, cancelamento, correlação e
isolamento de workflows concorrentes, agentes escalados e leases.

Validação local de 03/09/2026: 32 cenários novos; suíte Python com 568 aprovados
e 25 ignorados, 5 testes JavaScript aprovados, Ruff e mypy sem erros. Inclui uma
execução do grafo real com checkpoint em memória, ferramentas locais e provider
simulado, passando por planner, executor bloqueado e judge. Os testes dependentes
de serviços externos/Docker não foram executados nesta rodada; estes números não
são uma certificação de produção nem uma medição com LLM real.
