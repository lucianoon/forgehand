"""Ferramentas de exploração para os agentes + loop de tool-use.

Antes disto cada agente era UMA chamada de completion: o executor não conseguia
abrir um arquivo fora do grounding nem saber o que já falhava; o judge só via o
resumo do executor. Aqui os agentes ganham um conjunto pequeno e auditável de
ferramentas (ler arquivo, listar diretório, buscar no repositório, rodar uma
verificação configurada) e um loop com teto de chamadas e de tokens.

Regras que se mantêm:
- regra 1: a única porta para o LLM continua sendo o ProviderRouter; o loop
  fala CompletionRequest/CompletionResult e não conhece SDK;
- regra 2: a resposta final continua sendo saída estruturada — o modelo termina
  chamando `emit_structured_output`, nunca com texto solto;
- segurança: toda leitura fica dentro do root, diretórios ignorados e arquivos
  sensíveis (.env, chaves) são bloqueados, e `run_check` só executa comandos
  que já passaram pelo allowlist do CommandPolicy.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from app.agents.hooks import ToolHookCall, ToolHookDispatcher
from app.agents.validation import ValidationSignal
from app.infrastructure.repository_grounding import IGNORED_DIRS, TEXT_EXTENSIONS
from app.providers.base import (
    CompletionRequest,
    CompletionResult,
    Message,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from app.providers.registry import ModelTier, ProviderRouter

_SENSITIVE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "*.secret",
    "*credentials*",
)
_MAX_LIST_ENTRIES = 200


class ToolError(Exception):
    """Erro devolvido ao modelo como tool_result com is_error — o modelo pode
    corrigir a chamada. Nunca derruba o workflow."""


class AgentTool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]

    async def run(self, arguments: dict[str, Any]) -> str: ...


class ObjectiveCheck(Protocol):
    """O que run_check precisa de um validador: nome e execução incondicional.
    Protocolo (e não a classe concreta) para não importar workspace_runtime
    aqui — ele importa executor, que importa este módulo."""

    name: str

    async def execute(self) -> ValidationSignal: ...


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncado: {len(text) - limit} caracteres omitidos]"


def _is_sensitive(path: Path) -> bool:
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in _SENSITIVE_PATTERNS)


class _WorkspaceBound:
    """Resolução de path compartilhada: dentro do root, fora de dirs ignorados
    e de arquivos sensíveis. Mesma política do LocalWorkspaceRuntime."""

    def __init__(self, root: str) -> None:
        self._root = Path(root).expanduser().resolve()

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, raw_path: Any, *, allow_root: bool = False) -> Path:
        if raw_path is None or raw_path == "":
            if allow_root:
                return self._root
            raise ToolError("`path` é obrigatório.")
        if not isinstance(raw_path, str):
            raise ToolError("`path` deve ser uma string relativa à raiz do projeto.")
        candidate = (self._root / raw_path).resolve()
        try:
            relative = candidate.relative_to(self._root)
        except ValueError as exc:
            raise ToolError(f"path fora do workspace permitido: {raw_path}") from exc
        if any(part in IGNORED_DIRS for part in relative.parts):
            raise ToolError(f"diretório ignorado: {raw_path}")
        if candidate != self._root and _is_sensitive(candidate):
            raise ToolError(f"arquivo sensível, leitura bloqueada: {raw_path}")
        return candidate


class ReadFileTool(_WorkspaceBound):
    name = "read_file"
    description = (
        "Lê um arquivo do workspace com numeração de linhas. Use antes de um "
        "op=replace para copiar o trecho exato. Opcionalmente restrinja a um "
        "intervalo de linhas."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relativo à raiz."},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
    }

    def __init__(self, root: str, *, max_output_chars: int = 12_000) -> None:
        super().__init__(root)
        self._max_output_chars = max_output_chars

    async def run(self, arguments: dict[str, Any]) -> str:
        path = self.resolve(arguments.get("path"))
        if not path.exists():
            raise ToolError(f"arquivo não encontrado: {arguments.get('path')}")
        if path.is_dir():
            raise ToolError("path é um diretório; use list_directory.")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError("arquivo binário ou fora de UTF-8.") from exc
        lines = text.splitlines()
        start = int(arguments.get("start_line") or 1)
        end = int(arguments.get("end_line") or len(lines))
        if start < 1 or end < start:
            raise ToolError("intervalo de linhas inválido.")
        selected = lines[start - 1 : end]
        width = len(str(min(end, len(lines))))
        body = "\n".join(
            f"{index:>{width}}| {line}" for index, line in enumerate(selected, start)
        )
        header = f"{path.relative_to(self.root).as_posix()} ({len(lines)} linhas)"
        return _truncate(f"{header}\n{body}", self._max_output_chars)


class ListDirectoryTool(_WorkspaceBound):
    name = "list_directory"
    description = (
        "Lista arquivos e subdiretórios de um diretório do workspace "
        "(diretórios terminam com '/'). Omita `path` para a raiz."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
    }

    async def run(self, arguments: dict[str, Any]) -> str:
        path = self.resolve(arguments.get("path"), allow_root=True)
        if not path.is_dir():
            raise ToolError(f"não é um diretório: {arguments.get('path')}")
        entries = sorted(
            (
                entry
                for entry in path.iterdir()
                if entry.name not in IGNORED_DIRS and not _is_sensitive(entry)
            ),
            key=lambda item: (not item.is_dir(), item.name.lower()),
        )
        lines = [f"{e.name}/" if e.is_dir() else e.name for e in entries]
        omitted = len(lines) - _MAX_LIST_ENTRIES
        lines = lines[:_MAX_LIST_ENTRIES]
        if omitted > 0:
            lines.append(f"... [{omitted} entradas omitidas]")
        rel = path.relative_to(self.root).as_posix() or "."
        return f"{rel}\n" + "\n".join(lines)


class SearchRepositoryTool(_WorkspaceBound):
    name = "search_repository"
    description = (
        "Busca uma expressão regular (sintaxe Python) em arquivos de texto do "
        "workspace e devolve `path:linha: texto`. Use para localizar símbolos, "
        "usos e definições antes de editar."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex Python."},
            "path_prefix": {
                "type": "string",
                "description": "Restringe a busca a este diretório relativo.",
            },
            "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "required": ["pattern"],
    }

    def __init__(
        self,
        root: str,
        *,
        max_results: int = 50,
        max_output_chars: int = 12_000,
        max_file_bytes: int = 256_000,
    ) -> None:
        super().__init__(root)
        self._max_results = max_results
        self._max_output_chars = max_output_chars
        self._max_file_bytes = max_file_bytes

    async def run(self, arguments: dict[str, Any]) -> str:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ToolError("`pattern` é obrigatório.")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"regex inválida: {exc}") from exc
        limit = min(int(arguments.get("max_results") or self._max_results), 200)
        base = self.resolve(arguments.get("path_prefix"), allow_root=True)
        if not base.exists():
            raise ToolError(f"path_prefix não existe: {arguments.get('path_prefix')}")

        hits: list[str] = []
        scanned = 0
        for path in sorted(base.rglob("*") if base.is_dir() else [base]):
            if not path.is_file():
                continue
            relative = path.relative_to(self.root)
            if any(part in IGNORED_DIRS for part in relative.parts):
                continue
            if _is_sensitive(path) or path.suffix.lower() not in TEXT_EXTENSIONS:
                continue
            if path.stat().st_size > self._max_file_bytes:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            scanned += 1
            for number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{relative.as_posix()}:{number}: {line.strip()}")
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit:
                break
        if not hits:
            return f"nenhuma ocorrência de /{pattern}/ em {scanned} arquivos."
        header = f"{len(hits)} ocorrência(s)" + (
            f" (limite {limit} atingido)" if len(hits) >= limit else ""
        )
        return _truncate(header + "\n" + "\n".join(hits), self._max_output_chars)


class RunCheckTool:
    """Executa uma das verificações objetivas já configuradas (pytest, ruff,
    mypy...). Só comandos que passaram pelo CommandPolicy — o modelo escolhe o
    NOME, nunca o comando."""

    name = "run_check"

    def __init__(
        self,
        validators: list[ObjectiveCheck],
        *,
        max_output_chars: int = 12_000,
    ) -> None:
        self._validators = {validator.name: validator for validator in validators}
        self._max_output_chars = max_output_chars
        names = sorted(self._validators)
        self.description = (
            "Roda uma verificação objetiva configurada no workspace atual e "
            "devolve exit code e saída. Útil para saber o que já falha antes de "
            f"editar. Disponíveis: {', '.join(names)}."
        )
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {"name": {"type": "string", "enum": names}},
            "required": ["name"],
        }

    async def run(self, arguments: dict[str, Any]) -> str:
        name = arguments.get("name")
        validator = self._validators.get(str(name))
        if validator is None:
            raise ToolError(
                f"verificação desconhecida: {name}. Disponíveis: "
                f"{', '.join(sorted(self._validators))}."
            )
        signal = await validator.execute()
        status = "passed" if signal.passed else "failed"
        parts = [f"{signal.name}: {status} (exit_code={signal.exit_code})"]
        if signal.command:
            parts.append(f"command: {signal.command}")
        if signal.stdout.strip():
            parts.append(f"stdout:\n{signal.stdout.rstrip()}")
        if signal.stderr.strip():
            parts.append(f"stderr:\n{signal.stderr.rstrip()}")
        return _truncate("\n".join(parts), self._max_output_chars)


def build_workspace_tools(
    root: str,
    *,
    max_output_chars: int = 12_000,
    validators: list[ObjectiveCheck] | None = None,
) -> list[AgentTool]:
    tools: list[AgentTool] = [
        ReadFileTool(root, max_output_chars=max_output_chars),
        ListDirectoryTool(root),
        SearchRepositoryTool(root, max_output_chars=max_output_chars),
    ]
    if validators:
        tools.append(RunCheckTool(validators, max_output_chars=max_output_chars))
    return tools


def tool_specs(tools: list[AgentTool]) -> list[ToolSpec]:
    return [
        ToolSpec(name=t.name, description=t.description, input_schema=t.input_schema)
        for t in tools
    ]


@dataclass
class ToolLoopOutcome:
    result: CompletionResult
    tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    rounds: int = 1
    stopped_reason: str = "final_answer"
    trace: list[dict[str, Any]] = field(default_factory=list)

    def exploration_summary(self) -> dict[str, Any]:
        """Vai para o payload da tarefa: rastreável no checkpoint e visível ao
        judge/advisor sem inundar o prompt (só previews)."""
        return {
            "tool_calls": self.tool_calls,
            "rounds": self.rounds,
            "stopped_reason": self.stopped_reason,
            "trace": self.trace,
        }


_FINAL_ANSWER_NOTE = (
    "Limite de exploração atingido. Produza AGORA a resposta final estruturada "
    "com o que você já sabe; não chame mais ferramentas."
)


class ToolLoop:
    """Executa completions até o modelo emitir a resposta estruturada.

    Cada rodada: chama o router; se vier `parsed`, acabou; se vierem
    tool_calls, executa cada uma, anexa assistant(tool_calls) + user
    (tool_results) e repete. Ao atingir max_tool_calls ou o teto de tokens, a
    rodada seguinte força a resposta final (`force_final`), mantendo as
    ferramentas declaradas — a API exige que tool_use/tool_result no histórico
    tenham as definições presentes."""

    def __init__(
        self,
        router: ProviderRouter,
        tools: list[AgentTool] | None = None,
        *,
        max_tool_calls: int = 8,
        preview_chars: int = 200,
        hooks: ToolHookDispatcher | None = None,
        agent_name: str = "agent",
    ) -> None:
        self._router = router
        self._tools = {tool.name: tool for tool in (tools or [])}
        self._specs = tool_specs(list(self._tools.values()))
        self._max_tool_calls = max(0, max_tool_calls)
        self._preview_chars = preview_chars
        self._hooks = hooks
        self._agent_name = agent_name

    @property
    def has_tools(self) -> bool:
        return bool(self._specs) and self._max_tool_calls > 0

    def has_tool(self, name: str) -> bool:
        return self.has_tools and name in self._tools

    async def run(
        self,
        tier: ModelTier | None,
        request: CompletionRequest,
        *,
        token_ceiling: int | None = None,
        task_id: str | None = None,
        refresh_paths: list[str] | None = None,
    ) -> ToolLoopOutcome:
        if not self.has_tools:
            result = await self._router.complete(tier, request)
            return ToolLoopOutcome(
                result=result,
                tokens=result.usage.total_tokens,
                cost_usd=result.cost_usd,
                stopped_reason="no_tools",
            )

        messages = list(request.messages)
        tokens = 0
        cost = 0.0
        calls_made = 0
        rounds = 0
        trace: list[dict[str, Any]] = []
        force_final = False
        stopped_reason = "final_answer"
        run_id = str(uuid4())

        # Controller-selected recovery reads use the same confinement, hooks
        # and call budget as model-selected reads. Never create a second reader
        # from paths/roots supplied in checkpoint or model payloads.
        if refresh_paths and self.has_tool("read_file"):
            refresh_calls: list[ToolCall] = []
            refresh_results: list[ToolResult] = []
            for path in dict.fromkeys(refresh_paths):
                if calls_made >= min(4, self._max_tool_calls) or (
                    token_ceiling is not None and token_ceiling <= 0
                ):
                    break
                calls_made += 1
                call = ToolCall(
                    id=f"refresh-{run_id}-{calls_made}",
                    name="read_file",
                    arguments={"path": path},
                )
                content, ok = await self._execute_with_hooks(
                    call,
                    ToolHookCall(
                        run_id=run_id,
                        ordinal=calls_made,
                        tool="read_file",
                        agent=self._agent_name,
                        task_id=task_id,
                    ),
                )
                refresh_calls.append(call)
                refresh_results.append(
                    ToolResult(
                        tool_call_id=call.id,
                        name=call.name,
                        content=content,
                        is_error=not ok,
                    )
                )
                trace.append(
                    {
                        "round": 0,
                        "name": call.name,
                        "arguments": call.arguments,
                        "ok": ok,
                        "chars": len(content),
                        "preview": content[: self._preview_chars],
                        "source": "recovery_refresh",
                    }
                )
            if refresh_calls:
                force_final = calls_made >= self._max_tool_calls
                if force_final:
                    stopped_reason = "max_tool_calls"
                messages.extend(
                    [
                        Message(
                            role="assistant",
                            content="Leituras de recuperação solicitadas pelo controlador.",
                            tool_calls=refresh_calls,
                        ),
                        Message(
                            role="user",
                            tool_results=refresh_results,
                            content=(
                                "Leituras atuais dos arquivos envolvidos na tentativa anterior. "
                                "Use as leituras bem-sucedidas para copiar código; erros de "
                                "leitura e stdout/stderr de comandos não são código-fonte. "
                                "A numeração de linhas não faz parte do arquivo. "
                                "O grounding inicial pode estar desatualizado."
                                + ("\n" + _FINAL_ANSWER_NOTE if force_final else "")
                            ),
                        ),
                    ]
                )

        while True:
            rounds += 1
            result = await self._router.complete(
                tier,
                request.model_copy(
                    update={
                        "messages": messages,
                        "tools": self._specs,
                        "force_final": force_final,
                    }
                ),
            )
            tokens += result.usage.total_tokens
            cost += result.cost_usd
            if result.parsed is not None or not result.tool_calls:
                break
            if force_final:
                raise ToolError(
                    "Provider ignored final-answer limit; tool loop stopped."
                )

            tool_results: list[ToolResult] = []
            for call in result.tool_calls:
                if calls_made >= self._max_tool_calls or (
                    token_ceiling is not None and tokens >= token_ceiling
                ):
                    content, ok = (
                        "Limite de exploração atingido; ferramenta não executada.",
                        False,
                    )
                else:
                    calls_made += 1
                    hook_call = ToolHookCall(
                        run_id=run_id,
                        ordinal=calls_made,
                        tool=call.name if call.name in self._tools else "<unknown>",
                        agent=self._agent_name,
                        task_id=task_id,
                    )
                    content, ok = await self._execute_with_hooks(call, hook_call)
                trace.append(
                    {
                        "round": rounds,
                        "name": call.name,
                        "arguments": call.arguments,
                        "ok": ok,
                        "chars": len(content),
                        "preview": content[: self._preview_chars],
                    }
                )
                tool_results.append(
                    ToolResult(
                        tool_call_id=call.id,
                        name=call.name,
                        content=content,
                        is_error=not ok,
                    )
                )

            budget_hit = token_ceiling is not None and tokens >= token_ceiling
            calls_hit = calls_made >= self._max_tool_calls
            if budget_hit or calls_hit:
                force_final = True
                stopped_reason = "token_ceiling" if budget_hit else "max_tool_calls"
            messages.append(
                Message(
                    role="assistant", content=result.text, tool_calls=result.tool_calls
                )
            )
            messages.append(
                Message(
                    role="user",
                    content=_FINAL_ANSWER_NOTE if force_final else "",
                    tool_results=tool_results,
                )
            )

        return ToolLoopOutcome(
            result=result,
            tokens=tokens,
            cost_usd=cost,
            tool_calls=calls_made,
            rounds=rounds,
            stopped_reason=stopped_reason,
            trace=trace,
        )

    async def _execute_with_hooks(
        self, call: ToolCall, hook_call: ToolHookCall
    ) -> tuple[str, bool]:
        if self._hooks is None:
            return await self._execute(call)
        if not await self._hooks.dispatch("pre_tool", hook_call):
            return "Ferramenta bloqueada pela política do operador.", False
        content, ok = await self._execute(call)
        if not ok:
            await self._hooks.dispatch("tool_error", hook_call, outcome="failed")
            return content, False
        if not await self._hooks.dispatch(
            "post_tool", hook_call, output_chars=len(content), outcome="succeeded"
        ):
            return (
                "Resultado ocultado pela política do operador; ação não desfeita.",
                False,
            )
        return content, True

    async def _execute(self, call: ToolCall) -> tuple[str, bool]:
        tool = self._tools.get(call.name)
        if tool is None:
            return (
                f"ferramenta desconhecida: {call.name}. Disponíveis: "
                f"{', '.join(sorted(self._tools))}.",
                False,
            )
        try:
            return await tool.run(dict(call.arguments)), True
        except ToolError as exc:
            return str(exc), False
        except Exception as exc:  # noqa: BLE001 — erro interno vira feedback
            return f"erro interno em {call.name}: {type(exc).__name__}: {exc}", False
