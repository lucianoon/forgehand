from __future__ import annotations

import asyncio
import difflib
import os
import re
import shlex
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from app.agents.executor import ExecutionStrategy, LLMExecutor
from app.agents.validation import ObjectiveValidationPipeline, ValidationSignal
from app.infrastructure.command_policy import CommandPolicy as CommandPolicy
from app.models.task import AgentTask, Capability
from app.factory.lifecycle import inherited_lock_fds
from app.infrastructure.posix import kill_process_group


class CommandRunner(Protocol):
    async def run(
        self, command: str, workspace_root: Path, output_limit: int
    ) -> dict[str, Any]: ...


def resolve_argv(argv: list[str]) -> list[str]:
    """Troca argv[0] pelo caminho absoluto encontrado no PATH.

    No Windows, CreateProcess procura primeiro no diretório do executável do
    processo pai: um servidor rodando sob `uv run` que chama `python -m pytest`
    cai no interpretador base (sem pytest) em vez do da venv que está no PATH.
    Resolver antes garante o mesmo binário que `shutil.which` (e o operador)
    enxergam. Sem correspondência, mantém o nome e deixa o SO falhar.
    """
    if not argv:
        return argv
    located = shutil.which(argv[0])
    return [located, *argv[1:]] if located else list(argv)


PYTEST_NO_TESTS_COLLECTED = 5


_SECRET_ENV_PATTERN = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)


def sanitized_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Ambiente sem segredos para processos filhos: remove variáveis cujo nome
    sugere chave, token, senha ou credencial. PATH e o resto ficam."""
    base = os.environ if source is None else source
    return {k: v for k, v in base.items() if not _SECRET_ENV_PATTERN.search(k)}


class LocalCommandRunner:
    def __init__(
        self,
        policy: CommandPolicy | None = None,
        *,
        timeout_seconds: float | None = None,
        sanitize_env: bool = False,
    ) -> None:
        self._policy = policy or CommandPolicy()
        self._timeout_seconds = timeout_seconds
        self._sanitize_env = sanitize_env

    async def run(
        self, command: str, workspace_root: Path, output_limit: int
    ) -> dict[str, Any]:
        argv = resolve_argv(self._policy.parse(command))
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(workspace_root),
            env=sanitized_environment() if self._sanitize_env else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout_seconds
            )
        except (TimeoutError, asyncio.CancelledError) as exc:
            kill_process_group(process)
            await asyncio.shield(process.wait())
            if isinstance(exc, asyncio.CancelledError):
                raise
            result = _command_result(
                command,
                None,
                b"",
                f"timeout após {self._timeout_seconds:g}s; processo encerrado".encode(),
                output_limit,
            )
            result["timed_out"] = True
            return result
        result = _command_result(
            command, process.returncode, stdout, stderr, output_limit
        )
        result["timed_out"] = False
        return result


class DockerSandboxCommandRunner:
    def __init__(
        self,
        *,
        image: str,
        memory: str = "512m",
        cpus: float = 1.0,
        network_enabled: bool = False,
        policy: CommandPolicy | None = None,
    ) -> None:
        self._image = image
        self._memory = memory
        self._cpus = cpus
        self._network_enabled = network_enabled
        self._policy = policy or CommandPolicy()

    def build_argv(self, command: str, workspace_root: Path) -> list[str]:
        command_argv = self._policy.parse(command)
        return [
            "docker",
            "run",
            "--rm",
            "--init",
            "--network",
            "bridge" if self._network_enabled else "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--memory",
            self._memory,
            "--cpus",
            str(self._cpus),
            "--pids-limit",
            "256",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "-v",
            f"{workspace_root}:/workspace:rw",
            "-w",
            "/workspace",
            self._image,
            *command_argv,
        ]

    async def run(
        self, command: str, workspace_root: Path, output_limit: int
    ) -> dict[str, Any]:
        argv = self.build_argv(command, workspace_root)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return _command_result(
            command, process.returncode, stdout, stderr, output_limit
        )


def _command_result(
    command: str,
    returncode: int | None,
    stdout: bytes,
    stderr: bytes,
    output_limit: int,
) -> dict[str, Any]:
    return {
        "command": command,
        "exit_code": returncode,
        "stdout": stdout.decode("utf-8", errors="ignore")[:output_limit],
        "stderr": stderr.decode("utf-8", errors="ignore")[:output_limit],
    }


class OperationApplyError(ValueError):
    """Falha SEMÂNTICA de uma operação (search não encontrado, ambíguo, arquivo
    inexistente). Não derruba a tarefa: vira feedback operacional para o
    autocorrect e sinal objetivo para o judge. Falhas de segurança (path fora
    do workspace) continuam sendo ValueError comum e abortam a tarefa."""


def normalize_operations(result_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """`operations` novo + `files` legado (arquivo inteiro → op=create), na
    ordem: legado primeiro, depois as operações — um payload não deveria
    misturar os dois, mas se misturar o replace vê o arquivo já criado."""
    operations: list[dict[str, Any]] = []
    legacy = result_payload.get("files")
    if isinstance(legacy, list):
        for artifact in legacy:
            if isinstance(artifact, dict):
                operations.append(
                    {
                        "op": "create",
                        "path": artifact.get("path"),
                        "content": artifact.get("content"),
                    }
                )
    declared = result_payload.get("operations")
    if isinstance(declared, list):
        operations.extend(item for item in declared if isinstance(item, dict))
    return operations


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for line in text.split("\n")[:-1]:
        offsets.append(offsets[-1] + len(line) + 1)
    return offsets


def _find_line_tolerant_spans(
    text: str, search: str, *, ignore_indent: bool = False
) -> list[tuple[int, int]]:
    """Casamento linha a linha ignorando espaços à direita — cobre CRLF e
    trailing whitespace, os erros mais comuns quando o modelo copia um trecho.
    Com ignore_indent, também ignora a indentação à esquerda (o trecho foi
    copiado de uma evidência com outro nível); o replace é reindentado."""
    text_lines = text.split("\n")
    def normalize(line: str) -> str:
        return line.strip() if ignore_indent else line.rstrip()

    search_lines = [normalize(line) for line in search.strip("\n").split("\n")]
    if not search_lines or not any(search_lines):
        return []
    offsets = _line_offsets(text)
    spans: list[tuple[int, int]] = []
    width = len(search_lines)
    for index in range(len(text_lines) - width + 1):
        window = text_lines[index : index + width]
        if all(normalize(a) == b for a, b in zip(window, search_lines, strict=True)):
            start = offsets[index]
            end = offsets[index + width - 1] + len(text_lines[index + width - 1])
            spans.append((start, end))
    return spans


def _first_indent(block: str) -> str:
    for line in block.split("\n"):
        if line.strip():
            return line[: len(line) - len(line.lstrip())]
    return ""


def reindent_replacement(original: str, search: str, replacement: str) -> str:
    """Leva o `replace` para a indentação real do trecho encontrado quando o
    `search` veio com outro nível (casamento tolerante à indentação)."""
    target, source = _first_indent(original), _first_indent(search)
    if target == source:
        return replacement
    lines: list[str] = []
    for line in replacement.split("\n"):
        if not line.strip():
            lines.append(line)
        elif source and line.startswith(source):
            lines.append(target + line[len(source) :])
        elif not source:
            lines.append(target + line)
        else:
            lines.append(line)
    return "\n".join(lines)


def _find_replace_span_ex(
    text: str, search: str, occurrence: int | None
) -> tuple[int, int, bool]:
    """Localiza `search` em `text`. Exato primeiro; se não achar e o trecho é
    multilinha, tenta o casamento tolerante (espaços à direita; depois também
    indentação). Ambiguidade sem `occurrence` é erro: substituir "a primeira"
    silenciosamente é como bugs entram. O terceiro valor diz se a indentação
    foi ignorada (o replace precisa ser reindentado)."""
    if not search:
        raise OperationApplyError("`search` vazio.")
    indent_tolerant = False
    spans = [
        (match.start(), match.end()) for match in re.finditer(re.escape(search), text)
    ]
    if not spans and "\n" in search:
        spans = _find_line_tolerant_spans(text, search)
    if not spans and "\n" in search:
        spans = _find_line_tolerant_spans(text, search, ignore_indent=True)
        indent_tolerant = bool(spans)
    if not spans:
        raise OperationApplyError(
            "trecho `search` não encontrado no arquivo atual; copie-o "
            "literalmente das evidências (indentação e linhas iguais)."
        )
    if occurrence is None:
        if len(spans) > 1:
            raise OperationApplyError(
                f"trecho `search` aparece {len(spans)} vezes; amplie o trecho "
                "para torná-lo único ou informe `occurrence`."
            )
        start, end = spans[0]
        return start, end, indent_tolerant
    if occurrence > len(spans):
        raise OperationApplyError(
            f"`occurrence`={occurrence}, mas o trecho aparece apenas "
            f"{len(spans)} vez(es)."
        )
    start, end = spans[occurrence - 1]
    return start, end, indent_tolerant


def find_replace_span(
    text: str, search: str, occurrence: int | None
) -> tuple[int, int]:
    start, end, _ = _find_replace_span_ex(text, search, occurrence)
    return start, end


def apply_replace(
    before: str, search: str, replacement: str, occurrence: int | None
) -> str:
    start, end, indent_tolerant = _find_replace_span_ex(before, search, occurrence)
    if indent_tolerant:
        replacement = reindent_replacement(before[start:end], search, replacement)
    return before[:start] + replacement + before[end:]


class LocalWorkspaceRuntime:
    """Aplica operações do executor no workspace local de forma controlada."""

    def __init__(
        self,
        workspace_root: str,
        *,
        apply_files_enabled: bool = False,
        command_feedback_runners: list["CommandObjectiveValidator"] | None = None,
        validation_pipeline: ObjectiveValidationPipeline | None = None,
    ) -> None:
        self._root = Path(workspace_root).expanduser().resolve()
        self._apply_files_enabled = apply_files_enabled
        self._validation_pipeline = validation_pipeline or ObjectiveValidationPipeline(
            command_feedback_runners or []
        )

    async def apply(
        self,
        task: AgentTask,
        result_payload: dict[str, Any],
        strategy: ExecutionStrategy | None = None,
    ) -> dict[str, Any]:
        strategy = strategy or ExecutionStrategy()
        operations = normalize_operations(result_payload)
        previous = self._previous_workspace(task)
        if (
            not self._apply_files_enabled
            or not strategy.apply_files
            or (not operations and not previous)
        ):
            return {
                "workspace": {
                    "apply_files_enabled": self._apply_files_enabled
                    and strategy.apply_files,
                    "applied_files": [],
                    "workspace_root": str(self._root),
                    "strategy": strategy.model_dump(mode="json"),
                }
            }

        applied_files: list[str] = []
        file_diffs: list[dict[str, Any]] = []
        operation_history: list[dict[str, Any]] = []
        apply_errors: list[dict[str, Any]] = []
        published: dict[str, str] = {}
        originals: dict[str, str | None] = {}
        deleted_paths: list[str] = []
        for operation in operations:
            op = operation.get("op")
            path = self._resolve_artifact_path(operation.get("path"))
            relative_path = path.relative_to(self._root).as_posix()
            try:
                before, after = self._apply_operation(op, path, operation)
            except OperationApplyError as exc:
                apply_errors.append(
                    {"path": relative_path, "operation": op, "error": str(exc)}
                )
                operation_history.append(
                    {
                        "step": "apply_file",
                        "operation": op,
                        "path": relative_path,
                        "applied": False,
                        "error": str(exc),
                    }
                )
                continue
            applied_files.append(relative_path)
            originals.setdefault(relative_path, before)
            diff_entry = self._build_diff_entry(
                relative_path, originals[relative_path], after
            )
            diff_entry["operation"] = op
            file_diffs.append(diff_entry)
            if after is None:
                deleted_paths.append(relative_path)
                published.pop(relative_path, None)
            else:
                published[relative_path] = after
                deleted_paths = [item for item in deleted_paths if item != relative_path]
            operation_history.append(
                {
                    "step": "apply_file",
                    "operation": op,
                    "path": relative_path,
                    "change_type": diff_entry["change_type"],
                    "changed": diff_entry["changed"],
                }
            )

        evidence = LLMExecutor._merge_workspace_evidence(previous, {
            "applied_files": applied_files,
            "file_diffs": file_diffs,
            "published_files": [
                {"path": path, "content": content}
                for path, content in published.items()
            ],
            "deleted_paths": deleted_paths,
        })
        applied_files = evidence["applied_files"]
        command_feedback: list[ValidationSignal] = []
        if apply_errors:
            command_feedback.append(
                ValidationSignal(
                    name="apply",
                    passed=False,
                    details="; ".join(
                        f"{item['operation']} {item['path']}: {item['error']}"
                        for item in apply_errors
                    ),
                )
            )
        if strategy.run_objective_validation:
            command_feedback.extend(
                await self._run_command_feedback(
                    task,
                    applied_files=applied_files,
                )
            )
        for signal in command_feedback:
            operation_history.append(
                {
                    "step": "command_feedback",
                    "name": signal.name,
                    "passed": signal.passed,
                    "command": signal.command,
                    "exit_code": signal.exit_code,
                    "details": signal.details,
                }
            )
        git_snapshot = (
            await self._capture_git_snapshot(applied_files)
            if strategy.run_objective_validation
            else None
        )
        if git_snapshot is not None:
            operation_history.append(
                {
                    "step": "git_snapshot",
                    "is_git_repo": True,
                    "status": git_snapshot["status"],
                }
            )
        # Re-read after validation: commands may also change/remove an artifact.
        # Checkpoint contents and workspace_root never authorize filesystem reads.
        self._refresh_artifacts(evidence)
        prior_history = previous.get("operation_history", []) if previous else []
        return {
            "workspace": {
                "apply_files_enabled": True,
                "applied_files": applied_files,
                "workspace_root": str(self._root),
                "task_id": str(task.id),
                "strategy": strategy.model_dump(mode="json"),
                "command_feedback": [
                    signal.model_dump(mode="json") for signal in command_feedback
                ],
                "file_diffs": evidence.get("file_diffs", []),
                "apply_errors": apply_errors,
                # Conteúdo FINAL dos arquivos tocados — o que a publicação de PR
                # envia. Com replace o payload do executor não carrega o
                # arquivo inteiro, então ele precisa viver aqui.
                "published_files": evidence["published_files"],
                "deleted_paths": evidence["deleted_paths"],
                "operation_history": [*prior_history, *operation_history],
                "command_executions": [
                    {
                        "name": signal.name,
                        "command": signal.command,
                        "passed": signal.passed,
                        "exit_code": signal.exit_code,
                        "stdout": signal.stdout,
                        "stderr": signal.stderr,
                    }
                    for signal in command_feedback
                ],
                "git_snapshot": git_snapshot,
            }
        }

    @staticmethod
    def _previous_workspace(task: AgentTask) -> dict[str, Any] | None:
        if not isinstance(task.result, dict):
            return None
        workspace = task.result.get("workspace")
        if (
            not isinstance(workspace, dict)
            or workspace.get("task_id") != str(task.id)
            or workspace.get("apply_files_enabled") is not True
        ):
            return None
        return workspace

    def _refresh_artifacts(self, evidence: dict[str, Any]) -> None:
        diffs = {item["path"]: item for item in evidence.get("file_diffs", [])}
        previous_contents = {
            item["path"]: item["content"]
            for item in evidence.get("published_files", [])
        }
        published: list[dict[str, str]] = []
        deleted: list[str] = []
        refreshed: list[dict[str, Any]] = []
        retained_paths = set(diffs) | set(previous_contents) | set(
            evidence.get("deleted_paths", [])
        )
        for relative_path in evidence["applied_files"]:
            if relative_path not in retained_paths:
                continue  # Historical create/delete already cancelled out.
            path = self._resolve_artifact_path(relative_path)
            after = path.read_text(encoding="utf-8") if path.exists() else None
            old_diff = diffs.get(relative_path, {})
            if "before_content" in old_diff or old_diff.get("change_type") == "created":
                before = old_diff.get("before_content")
                if before is None and after is None:
                    continue  # Created then deleted: no net publication.
                diff = self._build_diff_entry(relative_path, before, after)
                if "operation" in old_diff:
                    diff["operation"] = old_diff["operation"]
            else:
                # Compatibility with older checkpoints: re-read bytes without
                # inventing a task-start baseline that was never recorded.
                diff = dict(old_diff)
                if after != previous_contents.get(relative_path):
                    diff.update(self._build_diff_entry(
                        relative_path, previous_contents.get(relative_path), after
                    ))
                    if old_diff.get("change_type") == "created" and after is None:
                        continue
            if after is None:
                deleted.append(relative_path)
            else:
                published.append({"path": relative_path, "content": after})
            if diff:
                refreshed.append(diff)
        evidence["file_diffs"] = refreshed
        evidence["published_files"] = published
        evidence["deleted_paths"] = deleted

    async def _run_command_feedback(
        self,
        task: AgentTask,
        *,
        applied_files: list[str],
    ) -> list[ValidationSignal]:
        signals: list[ValidationSignal] = []
        for runner in self._validation_pipeline.validators_for_capability(
            task.capability
        ):
            run = getattr(runner, "run", None)
            if not callable(run):
                continue
            signal = await run(
                capability=task.capability,
                applied_files=applied_files,
            )
            if signal is not None:
                signals.append(signal)
        return signals

    async def _capture_git_snapshot(
        self,
        applied_files: list[str],
    ) -> dict[str, Any] | None:
        if not applied_files:
            return None
        git_dir = self._root / ".git"
        if not git_dir.exists():
            return None
        status = await self._run_command(
            "git status --short",
            output_limit=4000,
        )
        diff = await self._run_command(
            "git diff --no-ext-diff --relative",
            output_limit=8000,
        )
        return {
            "status": status["stdout"],
            "status_exit_code": status["exit_code"],
            "diff": diff["stdout"],
            "diff_exit_code": diff["exit_code"],
        }

    async def _run_command(
        self,
        command: str,
        *,
        output_limit: int,
    ) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            *resolve_argv(shlex.split(command)),
            cwd=str(self._root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=inherited_lock_fds(),
            start_new_session=True,
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            kill_process_group(process)
            await asyncio.shield(process.wait())
            raise
        return {
            "command": command,
            "exit_code": process.returncode,
            "stdout": stdout.decode("utf-8", errors="ignore")[:output_limit],
            "stderr": stderr.decode("utf-8", errors="ignore")[:output_limit],
        }

    @staticmethod
    def _apply_operation(
        op: Any, path: Path, operation: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """Executa uma operação e devolve (before, after). after=None é remoção."""
        before = path.read_text(encoding="utf-8") if path.exists() else None
        if op == "create":
            content = operation.get("content")
            if not isinstance(content, str):
                raise ValueError(f"Operação create em {path.name} sem content.")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return before, content
        if op == "replace":
            if before is None:
                raise OperationApplyError(
                    "arquivo não existe; use op=create para arquivos novos."
                )
            search = operation.get("search")
            replacement = operation.get("replace")
            if not isinstance(search, str) or not isinstance(replacement, str):
                raise ValueError(f"Operação replace em {path.name} malformada.")
            occurrence = operation.get("occurrence")
            after = apply_replace(
                before,
                search,
                replacement,
                occurrence if isinstance(occurrence, int) else None,
            )
            path.write_text(after, encoding="utf-8")
            return before, after
        if op == "delete":
            if before is None:
                raise OperationApplyError("arquivo não existe; nada a remover.")
            path.unlink()
            return before, None
        raise ValueError(f"Operação desconhecida: {op!r}")

    def _resolve_artifact_path(self, raw_path: Any) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("Artefato sem path relativo válido.")
        candidate = (self._root / raw_path).resolve()
        if not self._is_within_root(candidate):
            raise ValueError(f"Path fora do workspace permitido: {raw_path}")
        return candidate

    def _is_within_root(self, path: Path) -> bool:
        try:
            path.relative_to(self._root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _build_diff_entry(
        relative_path: str,
        before: str | None,
        after: str | None,
    ) -> dict[str, Any]:
        before_text = before or ""
        after_text = after or ""
        changed = before != after
        if after is None:
            change_type = "deleted"
        elif before is None:
            change_type = "created"
        else:
            change_type = "modified" if changed else "unchanged"
        diff_lines = list(
            difflib.unified_diff(
                before_text.splitlines(),
                after_text.splitlines(),
                fromfile=f"a/{relative_path}",
                tofile=f"b/{relative_path}",
                lineterm="",
            )
        )
        return {
            "path": relative_path,
            "before_content": before,
            "change_type": change_type,
            "changed": changed,
            "diff": "\n".join(diff_lines)[:8000],
        }


class CommandObjectiveValidator:
    def __init__(
        self,
        *,
        name: str,
        command: str,
        workspace_root: str,
        file_suffixes: set[str] | None = None,
        capabilities: set[Capability] | None = None,
        output_limit: int = 4000,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.name = name
        self._command = command
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._file_suffixes = {suffix.lower() for suffix in (file_suffixes or set())}
        self._capabilities = capabilities or set()
        self._output_limit = output_limit
        self._command_runner = command_runner or LocalCommandRunner()

    async def validate(self, task: AgentTask) -> ValidationSignal:
        cached_signal = self._cached_signal(task)
        if cached_signal is not None:
            return cached_signal

        applied_files = self._applied_files(task)
        signal = await self.run(
            capability=task.capability,
            applied_files=applied_files,
        )
        if signal is None:
            return ValidationSignal(
                name=self.name,
                passed=None,
                details="skipped: tarefa fora do escopo do validador",
            )
        return signal

    async def run(
        self,
        *,
        capability: Capability,
        applied_files: list[str],
    ) -> ValidationSignal | None:
        if not self._should_run(capability=capability, applied_files=applied_files):
            return None
        return await self.execute()

    async def execute(self) -> ValidationSignal:
        """Roda o comando incondicionalmente (usado pela ferramenta run_check
        dos agentes). `run` aplica o filtro de capability/sufixo antes."""
        execution = await self._command_runner.run(
            self._command, self._workspace_root, self._output_limit
        )
        stdout_text = str(execution["stdout"])
        stderr_text = str(execution["stderr"])
        combined = "\n".join(
            part.strip() for part in (stdout_text, stderr_text) if part.strip()
        )
        exit_code = execution["exit_code"]
        passed: bool | None = exit_code == 0
        if self.name == "pytest" and exit_code == PYTEST_NO_TESTS_COLLECTED:
            # Nenhum teste coletado não é código quebrado: sinal ausente, não
            # reprovação. Evita uma rodada de autocorreção inútil e deixa o
            # critério tests_pass sem evidência (fail-closed no judge).
            passed = None
            combined = f"nenhum teste coletado (exit_code={exit_code}). {combined}".strip()
        return ValidationSignal(
            name=self.name,
            passed=passed,
            details=combined or f"exit_code={exit_code}",
            command=self._command,
            exit_code=execution["exit_code"],
            stdout=stdout_text,
            stderr=stderr_text,
        )

    def _should_run(self, *, capability: Capability, applied_files: list[str]) -> bool:
        if self._capabilities and capability not in self._capabilities:
            return False
        if not applied_files:
            return False
        if not self._file_suffixes:
            return True
        return any(
            Path(path).suffix.lower() in self._file_suffixes for path in applied_files
        )

    def _cached_signal(self, task: AgentTask) -> ValidationSignal | None:
        if not isinstance(task.result, dict):
            return None
        workspace = task.result.get("workspace")
        if not isinstance(workspace, dict):
            return None
        feedback = workspace.get("command_feedback")
        if not isinstance(feedback, list):
            return None
        for item in feedback:
            if not isinstance(item, dict):
                continue
            if item.get("name") == self.name:
                return ValidationSignal.model_validate(item)
        return None

    @staticmethod
    def _applied_files(task: AgentTask) -> list[str]:
        if not isinstance(task.result, dict):
            return []
        workspace = task.result.get("workspace")
        if not isinstance(workspace, dict):
            return []
        applied_files = workspace.get("applied_files")
        if not isinstance(applied_files, list):
            return []
        return [path for path in applied_files if isinstance(path, str)]
