"""run_command — comandos livres (dentro de uma allowlist) para o executor.

run_check roda só os comandos fixos que o operador configurou. Esta ferramenta
dá profundidade ao executor sem abrir mão do controle: o modelo escolhe o
comando, mas

- o executável tem de estar na allowlist do CommandPolicy (git, python,
  pytest, ruff, mypy, uv); nunca há shell;
- subcomandos que tocam a rede ou instalam dependências são negados
  (git push/fetch/pull/clone/remote, uv add/sync/pip/publish, python -m pip);
- o processo roda no workspace, com timeout, sem as variáveis de ambiente
  sensíveis do servidor (chaves de API, tokens) e com saída truncada;
- com EXECUTOR_COMMAND_BACKEND=docker roda no sandbox sem rede.

Passa pelo ToolLoop, logo pelos hooks pre/post/error e pelos tetos do papel.
Opt-in: AGENT_TOOLS_ALLOW_COMMANDS=true, só para o executor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from app.agents.tools import ToolError
from app.infrastructure.command_policy import CommandPolicy


class CommandRunner(Protocol):
    """Mesmo contrato de app.infrastructure.workspace_runtime.CommandRunner.
    Redeclarado aqui para não importar workspace_runtime, que importa o
    executor, que importa este módulo (ciclo)."""

    async def run(
        self, command: str, workspace_root: Path, output_limit: int
    ) -> dict[str, Any]: ...

COMMAND_TOOL_GUIDANCE = """

run_command executa um comando da allowlist (git, python, pytest, ruff, mypy, \
uv) no workspace, sem shell e sem rede. Use-o para rodar um teste específico, \
inspecionar o git ou checar um módulo antes de entregar; não para instalar \
dependências. Comandos curtos e objetivos, poucas chamadas."""

# Subcomandos negados por executável: rede, instalação e mutação de remoto.
_DENIED_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "git": frozenset(
        {"push", "pull", "fetch", "clone", "remote", "submodule", "ls-remote", "lfs"}
    ),
    "uv": frozenset(
        {"add", "remove", "sync", "lock", "pip", "publish", "tool", "python", "self", "cache"}
    ),
}
_DENIED_PYTHON_MODULES = frozenset({"pip", "ensurepip", "venv", "http.server"})


def _basename(executable: str) -> str:
    name = Path(executable).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def check_command_policy(argv: list[str]) -> None:
    """Regras além da allowlist de executáveis. Levanta ToolError."""
    if not argv:
        raise ToolError("comando vazio.")
    executable = _basename(argv[0])
    denied = _DENIED_SUBCOMMANDS.get(executable)
    if denied:
        # Conservador: qualquer token posicional negado bloqueia, inclusive
        # depois de opções globais como `git -C <dir> fetch`.
        hit = next((arg for arg in argv[1:] if arg in denied), None)
        if hit is not None:
            raise ToolError(
                f"`{executable} {hit}` não é permitido: acesso à rede ou "
                "instalação de dependências ficam com o operador."
            )
    if executable in {"python", "python3"} and "-m" in argv:
        module = argv[argv.index("-m") + 1] if argv.index("-m") + 1 < len(argv) else ""
        if module in _DENIED_PYTHON_MODULES:
            raise ToolError(f"`python -m {module}` não é permitido.")
    if executable in {"python", "python3"} and "-c" in argv:
        raise ToolError(
            "`python -c` não é permitido: grave um arquivo no workspace e execute-o."
        )


class RunCommandTool:
    name = "run_command"

    def __init__(
        self,
        runner: CommandRunner,
        workspace_root: str | Path,
        *,
        policy: CommandPolicy | None = None,
        max_output_chars: int = 12_000,
    ) -> None:
        self._runner = runner
        self._root = Path(workspace_root).expanduser().resolve()
        self._policy = policy or CommandPolicy()
        self._max_output_chars = max_output_chars
        self.description = (
            "Executa um comando no workspace, sem shell e sem rede. Executáveis "
            "permitidos: git, python, pytest, ruff, mypy, uv. Negados: git "
            "push/pull/fetch/clone/remote, uv add/sync/pip, python -c e python -m "
            "pip. Devolve exit code, stdout e stderr (truncados)."
        )
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Linha de comando completa, ex.: 'python -m pytest tests/test_core.py -q'.",
                }
            },
            "required": ["command"],
        }

    async def run(self, arguments: dict[str, Any]) -> str:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolError("`command` é obrigatório.")
        try:
            argv = self._policy.parse(command.strip())
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        check_command_policy(argv)
        execution = await self._runner.run(
            command.strip(), self._root, self._max_output_chars
        )
        exit_code = execution.get("exit_code")
        status = (
            "timeout"
            if execution.get("timed_out")
            else ("passed" if exit_code == 0 else "failed")
        )
        parts = [f"{command.strip()}: {status} (exit_code={exit_code})"]
        stdout = str(execution.get("stdout") or "")
        stderr = str(execution.get("stderr") or "")
        if stdout.strip():
            parts.append(f"stdout:\n{stdout.rstrip()}")
        if stderr.strip():
            parts.append(f"stderr:\n{stderr.rstrip()}")
        text = "\n".join(parts)
        if len(text) <= self._max_output_chars:
            return text
        omitted = len(text) - self._max_output_chars
        return text[: self._max_output_chars] + f"\n... [truncado: {omitted} caracteres omitidos]"
