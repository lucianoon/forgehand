# Do zero ao primeiro workflow em 20 minutos

Sem Docker, sem banco. Windows, macOS ou Linux.

## 1. Instalar (2 min)

```bash
git clone https://github.com/lucianoon/forgehand && cd forgehand
uv sync --extra dev --locked
```

## 2. Configurar (3 min)

Crie um `forgehand.toml` na raiz (ou exporte as mesmas chaves como variáveis
de ambiente; a variável vence o arquivo):

```toml
llm_provider_backend = "anthropic"          # ou "openrouter" / "openai"
executor_workspace_root = "./data/demo-workspace"
repository_root = "./data/demo-workspace"
executor_apply_files_enabled = true
pytest_validation_command = "python -m pytest -q"
ruff_validation_command = "ruff check ."
agent_tools_allow_commands = true
web_references_enabled = true
```

Exporte a chave do provedor no ambiente do processo, nunca no arquivo:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## 3. Subir (1 min)

```bash
uv run uvicorn app.main:app --env-file .env.demo
```

Abra `http://localhost:8000/dashboard` com a chave `dev-key`. O `.env.demo`
força todos os backends para memória; o `forgehand.toml` completa o resto.

## 4. Primeiro workflow (5 min)

```bash
uv run forgehand run --project demo \
  --request "Crie o pacote Python sandbox_calc com soma, subtrai, multiplica e divide, testes pytest e pyproject mínimo; rode pytest e ruff antes de entregar." \
  --criterion "core.py criado com as quatro funções" \
  --criterion "os testes passam" \
  --budget-usd 0.5
```

O CLI acompanha as etapas em `stderr` e imprime a entrega em `stdout`. No
dashboard, a mesma execução aparece com tarefas, critérios, custo e a
exploração feita pelo executor (`run_command` rodando pytest e ruff).

## 5. Gate humano e cancelamento (2 min)

Quando o status for `awaiting_decision`:

```bash
uv run forgehand decide <workflow_id> retry        # ou accept_partial | abort
uv run forgehand cancel <workflow_id>
```

## 6. Medir (5 min, opcional, consome créditos)

```bash
uv run python -m app.evaluation.evals --budget-usd 1.0
```

Roda os casos de `evals/cases.json` em sequência, para na hora se o orçamento
acabar e escreve `reports/evals-latest.md` com conclusão, first pass, custo e
latência contra os limites de `evals/gates.json`. A linha de base publicada
está em `evals/baseline/`.

## Depois

- Entrega até PR verde no GitHub: `docs/integrations.md`.
- Factory mode (checkout isolado, sandbox Docker): `docs/factory-delivery.md`, Linux ou WSL.
- Todas as variáveis: `docs/configuration.md`.
