from __future__ import annotations

import asyncio
import difflib
import os
import shlex
from pathlib import Path
from typing import Any, Protocol

from app.agents.executor import ExecutionStrategy
from app.agents.validation import ObjectiveValidationPipeline, ValidationSignal
from app.models.task import AgentTask, Capability


class CommandRunner(Protocol):
    async def run(
        self, command: str, workspace_root: Path, output_limit: int
    ) -> dict[str, Any]: ...


# Operadores de shell. Só têm significado se alguém entregar o comando a um
# shell — coisa que nenhum runner faz mais (todos usam forma exec). Rejeitar
# aqui mantém a política honesta: `pytest && rm -rf /` passava no allowlist
# porque argv[0] era `pytest`.
_SHELL_OPERATORS = frozenset(
    {"&&", "||", "|", "|&", ";", ";;", "&", ">", ">>", ">|", "<", "<<", "<<<"}
)
_SHELL_SUBSTITUTION = ("$(", "`", "${")


class CommandPolicy:
    """Allowlist de executáveis para comandos de validação objetiva.

    A política pressupõe execução em forma exec (argv), nunca via shell: é o
    que garante que o executável validado aqui seja o executável que roda.
    """

    def __init__(self, allowed_executables: set[str] | None = None) -> None:
        self._allowed = allowed_executables or {
            "git",
            "mypy",
            "pytest",
            "python",
            "python3",
            "ruff",
            "uv",
        }

    @staticmethod
    def _normalize(executable: str) -> str:
        """Nome comparável ao allowlist.

        No Windows o mesmo binário aparece como ``python.exe`` e os nomes são
        case-insensitive; sem normalizar, todo comando é rejeitado lá. No POSIX
        o nome é comparado como está — ``python.exe`` seria outro arquivo.
        """
        if os.name != "nt":
            return executable
        name = executable.lower()
        return name[:-4] if name.endswith(".exe") else name

    def parse(self, command: str) -> list[str]:
        argv = shlex.split(command)
        if not argv:
            raise ValueError("Comando vazio.")
        executable = Path(argv[0]).name
        if self._normalize(executable) not in self._allowed:
            raise ValueError(f"Executável não permitido: {executable}")
        for token in argv[1:]:
            if token in _SHELL_OPERATORS:
                raise ValueError(f"Operador de shell não permitido no comando: {token}")
            if any(marker in token for marker in _SHELL_SUBSTITUTION):
                raise ValueError(
                    f"Substituição de shell não permitida no comando: {token}"
                )
        return argv


class LocalCommandRunner:
    def __init__(self, policy: CommandPolicy | None = None) -> None:
        self._policy = policy or CommandPolicy()

    async def run(
        self, command: str, workspace_root: Path, output_limit: int
    ) -> dict[str, Any]:
        argv = self._policy.parse(command)
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(workspace_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return _command_result(
            command, process.returncode, stdout, stderr, output_limit
        )


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
        # Forma exec: o argv validado pela política é entregue direto ao
        # container. Com `sh -lc <command>` o allowlist era decorativo — só
        # olhava argv[0] e o shell reinterpretava o resto da string.
        argv = self._policy.parse(command)
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            "bridge" if self._network_enabled else "none",
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
            *argv,
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


class LocalWorkspaceRuntime:
    """Aplica artefatos do executor no workspace local de forma controlada."""

    def __init__(
        self,
        workspace_root: str,
        *,
        apply_files_enabled: bool = False,
        command_feedback_runners: list["CommandObjectiveValidator"] | None = None,
        validation_pipeline: ObjectiveValidationPipeline | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self._root = Path(workspace_root).expanduser().resolve()
        self._apply_files_enabled = apply_files_enabled
        self._validation_pipeline = validation_pipeline or ObjectiveValidationPipeline(
            command_feedback_runners or []
        )
        # Mesmo runner da validação objetiva: o snapshot do git respeita a
        # mesma fronteira de execução. Antes ele usava um shell no host mesmo
        # com o backend docker configurado.
        self._command_runner = command_runner or LocalCommandRunner()

    async def apply(
        self,
        task: AgentTask,
        result_payload: dict[str, Any],
        strategy: ExecutionStrategy | None = None,
    ) -> dict[str, Any]:
        strategy = strategy or ExecutionStrategy()
        files = result_payload.get("files")
        if (
            not self._apply_files_enabled
            or not strategy.apply_files
            or not isinstance(files, list)
            or not files
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
        for artifact in files:
            if not isinstance(artifact, dict):
                continue
            path = self._resolve_artifact_path(artifact.get("path"))
            content = artifact.get("content")
            if not isinstance(content, str):
                raise ValueError(
                    f"Artefato inválido para {path.name}: content ausente."
                )
            before = path.read_text(encoding="utf-8") if path.exists() else None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            relative_path = path.relative_to(self._root).as_posix()
            applied_files.append(relative_path)
            diff_entry = self._build_diff_entry(relative_path, before, content)
            file_diffs.append(diff_entry)
            operation_history.append(
                {
                    "step": "apply_file",
                    "path": relative_path,
                    "change_type": diff_entry["change_type"],
                    "changed": diff_entry["changed"],
                }
            )

        command_feedback: list[ValidationSignal] = []
        if strategy.run_objective_validation:
            command_feedback = await self._run_command_feedback(
                task,
                applied_files=applied_files,
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
                "file_diffs": file_diffs,
                "operation_history": operation_history,
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
        # Snapshot é diagnóstico, não faz parte do veredito. Se o runner não
        # conseguir executá-lo (git ausente da imagem do sandbox, docker
        # indisponível), a tarefa segue sem ele em vez de falhar.
        try:
            status = await self._run_command(
                "git status --short",
                output_limit=4000,
            )
            diff = await self._run_command(
                "git diff --no-ext-diff --relative",
                output_limit=8000,
            )
        except (OSError, ValueError):
            return None
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
        return await self._command_runner.run(command, self._root, output_limit)

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
        after: str,
    ) -> dict[str, Any]:
        before_text = before or ""
        changed = before != after
        change_type = (
            "created" if before is None else "modified" if changed else "unchanged"
        )
        diff_lines = list(
            difflib.unified_diff(
                before_text.splitlines(),
                after.splitlines(),
                fromfile=f"a/{relative_path}",
                tofile=f"b/{relative_path}",
                lineterm="",
            )
        )
        return {
            "path": relative_path,
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

        execution = await self._command_runner.run(
            self._command, self._workspace_root, self._output_limit
        )
        stdout_text = str(execution["stdout"])
        stderr_text = str(execution["stderr"])
        combined = "\n".join(
            part.strip() for part in (stdout_text, stderr_text) if part.strip()
        )
        return ValidationSignal(
            name=self.name,
            passed=execution["exit_code"] == 0,
            details=combined or f"exit_code={execution['exit_code']}",
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
