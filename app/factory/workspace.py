"""Provisionamento de repositório e fronteira segura com o Git."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from app.factory.git_auth import GitAuthentication, GitHubRepositoryAccess
from app.factory.lifecycle import WorkspaceJournal, inherited_lock_fds
from app.infrastructure.posix import kill_process_group

from app.models.factory import (
    RepositoryTarget,
    WorkOrder,
    WorkspaceLease,
    WorkspaceLifecycle,
    WorkspaceRetention,
)


class WorkspaceManager(Protocol):
    async def provision(self, workflow_id: str, order: WorkOrder) -> WorkspaceLease:
        """Cria ou recupera a lease isolada do workflow."""
        ...

    async def reconstruct(self, lease: WorkspaceLease) -> WorkspaceLease:
        """Reconstrói recursos de processo após a retomada de um checkpoint."""
        ...

    async def cleanup(self, lease: WorkspaceLease) -> WorkspaceLease:
        """Libera o workspace local de forma idempotente."""
        ...


class WorkspaceRuntimeFactory(Protocol):
    def build_grounding(
        self, lease: WorkspaceLease, request: str
    ) -> dict[str, Any]: ...

    def build_planner(self, lease: WorkspaceLease) -> Any: ...

    def build_registry(self, lease: WorkspaceLease) -> Any: ...

    def build_judge(self, lease: WorkspaceLease) -> Any: ...


@dataclass(frozen=True)
class GitCommandResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str


class GitCommandError(RuntimeError):
    def __init__(self, result: GitCommandResult):
        self.result = result
        message = result.stderr or result.stdout or "git falhou sem saída"
        super().__init__(f"git retornou {result.exit_code}: {message}")


class SafeGitRunner:
    """Executa somente `git` por argv, com cwd confinada e ambiente mínimo."""

    _CREDENTIAL_URL = re.compile(r"(https?://)([^/@\s]+)@", re.IGNORECASE)

    def __init__(
        self,
        allowed_root: str | Path,
        *,
        timeout_seconds: float = 120,
        max_output_chars: int = 20_000,
        git_executable: str = "git",
    ) -> None:
        self._root = Path(allowed_root).expanduser().resolve()
        self._timeout = timeout_seconds
        self._max_output = max_output_chars
        self._git = git_executable

    @classmethod
    def redact(cls, value: str) -> str:
        return cls._CREDENTIAL_URL.sub(r"\1***@", value)

    async def run(
        self,
        args: list[str],
        *,
        cwd: str | Path | None = None,
        check: bool = True,
        authentication: GitAuthentication | None = None,
    ) -> GitCommandResult:
        if not args or any("\x00" in arg for arg in args):
            raise ValueError("Argumentos Git inválidos.")
        working_directory = self._resolve_cwd(cwd)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}
        }
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            }
        )
        # Stop discovery before an ancestor checkout can inject Git config.
        environment["GIT_CEILING_DIRECTORIES"] = str(working_directory.parent)
        command = args[2:] if args[0] == "--git-dir" else args
        remote_https = (
            bool(command)
            and command[0] in {"clone", "fetch", "ls-remote"}
            and any(arg.startswith("https://") for arg in command[1:])
        )
        if remote_https:
            if (working_directory / ".git").exists() or (
                (working_directory / "HEAD").exists()
                and (working_directory / "objects").is_dir()
                and (working_directory / "refs").is_dir()
            ):
                raise ValueError(
                    "Transporte remoto exige diretório de controle sem repositório."
                )
            # Anonymous retries must not use redirects to authorize cached private data.
            environment.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.followRedirects",
                    "GIT_CONFIG_VALUE_0": "false",
                }
            )
        if authentication is not None:
            if (
                not command
                or command[0] not in {"clone", "fetch", "ls-remote"}
                or authentication.source not in command
                or any(arg == "-c" or arg.startswith("--config") for arg in command)
                or (working_directory / ".git").exists()
            ):
                raise ValueError(
                    "Credencial permitida somente para transporte Git remoto."
                )
            environment.update(authentication.environment())
        process = await asyncio.create_subprocess_exec(
            self._git,
            *args,
            cwd=str(working_directory),
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=inherited_lock_fds(),
            start_new_session=True,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout
            )
        except (TimeoutError, asyncio.CancelledError) as exc:
            kill_process_group(process)
            await asyncio.shield(process.wait())
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise TimeoutError(f"git excedeu {self._timeout:g}s") from None

        def safe_output(value: bytes) -> str:
            decoded = value.decode("utf-8", errors="replace")
            if authentication is not None:
                decoded = authentication.redact(decoded)
            return self.redact(decoded)[: self._max_output]

        result = GitCommandResult(
            argv=(self._git, *args),
            exit_code=process.returncode or 0,
            stdout=safe_output(stdout_bytes),
            stderr=safe_output(stderr_bytes),
        )
        if check and result.exit_code != 0:
            raise GitCommandError(result)
        return result

    def _resolve_cwd(self, cwd: str | Path | None) -> Path:
        candidate = self._root if cwd is None else Path(cwd).expanduser().resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise ValueError("Diretório Git fora da raiz permitida.")
        if not candidate.is_dir():
            raise ValueError("Diretório Git não existe.")
        return candidate


class LocalGitWorkspaceManager:
    """Cache bare compartilhado + checkout gravável exclusivo por workflow."""

    _SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    _SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

    def __init__(
        self,
        root: str | Path,
        *,
        approved_hosts: list[str],
        runner: SafeGitRunner | None = None,
        repository_url_resolver: Callable[[RepositoryTarget], str] | None = None,
        allow_local_repositories: bool = False,
        repository_access: GitHubRepositoryAccess | None = None,
    ) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._cache_root = self._root / "cache"
        self._workspace_root = self._root / "workspaces"
        self._cache_root.mkdir(exist_ok=True)
        self._workspace_root.mkdir(exist_ok=True)
        self._approved_hosts = {host.lower().rstrip(".") for host in approved_hosts}
        self._runner = runner or SafeGitRunner(self._root)
        self._resolve_url = repository_url_resolver or self._default_url
        self._allow_local = allow_local_repositories
        self._repository_access = repository_access
        self._locks: dict[str, asyncio.Lock] = {}
        self.journal = WorkspaceJournal(self._root / "control")

    def transition(
        self, lease: WorkspaceLease, state: WorkspaceLifecycle, **updates: Any
    ) -> WorkspaceLease:
        changed = lease.model_copy(
            update={"state": state, "updated_at": datetime.now(timezone.utc), **updates}
        )
        self.journal.save(changed)
        return changed

    def retain(
        self, lease: WorkspaceLease, seconds: int, reason: str
    ) -> WorkspaceLease:
        return self.transition(
            lease,
            WorkspaceLifecycle.RETAINED,
            retention=WorkspaceRetention(
                retain_until=datetime.now(timezone.utc) + timedelta(seconds=seconds),
                reason=reason,
            ),
        )

    async def provision(self, workflow_id: str, order: WorkOrder) -> WorkspaceLease:
        self._validate_identifier(workflow_id, "workflow_id")
        existing = self.journal.get(workflow_id)
        if existing is not None:
            if existing.repository != order.repository:
                raise ValueError("Workspace ownership mismatch")
            return await self.reconstruct(existing)
        self._validate_ref(order.repository.base_ref)
        source = self._resolve_url(order.repository)
        self._validate_source(source, order.repository)
        cache = self._cache_path(order.repository)
        workspace = (self._workspace_root / workflow_id).resolve()
        branch = f"forgehand/{workflow_id}"

        lock = self._locks.setdefault(
            order.repository.full_name.lower(), asyncio.Lock()
        )
        async with lock:
            await self._validate_cache(cache, source)
            if not cache.exists():
                await self._run_remote(
                    [
                        "clone",
                        "--bare",
                        "--no-tags",
                        source,
                        str(cache),
                    ],
                    order.repository,
                    source,
                )
            await self._materialize_partial_cache(cache, order.repository, source)
            remote_ref = f"refs/remotes/origin/{order.repository.base_ref}"
            await self._run_remote(
                [
                    "--git-dir",
                    str(cache),
                    "fetch",
                    "--prune",
                    "--no-tags",
                    source,
                    f"+refs/heads/{order.repository.base_ref}:{remote_ref}",
                ],
                order.repository,
                source,
            )
            resolved = await self._runner.run(
                ["--git-dir", str(cache), "rev-parse", "--verify", remote_ref]
            )
            base_sha = resolved.stdout.strip()
            if (
                order.repository.expected_base_sha is not None
                and base_sha != order.repository.expected_base_sha
            ):
                raise ValueError("fixture_base_sha_mismatch")
            if not re.fullmatch(r"[0-9a-fA-F]{40,64}", base_sha):
                raise GitCommandError(resolved)
            if workspace.exists():
                return await self._lease_for_existing(
                    workflow_id, order, workspace, branch, base_sha
                )
            lease = WorkspaceLease(
                workflow_id=workflow_id,
                repository=order.repository,
                local_path=str(workspace),
                branch=branch,
                base_sha=base_sha,
            )
            self.transition(lease, WorkspaceLifecycle.REQUESTED)
            self.transition(lease, WorkspaceLifecycle.PROVISIONING)
            await self._runner.run(
                ["clone", "--no-hardlinks", str(cache), str(workspace)]
            )
            await self._runner.run(["checkout", "-B", branch, base_sha], cwd=workspace)

        return self.transition(lease, WorkspaceLifecycle.READY)

    async def _materialize_partial_cache(
        self, cache: Path, repository: RepositoryTarget, source: str
    ) -> None:
        partial = await self._runner.run(
            ["--git-dir", str(cache), "config", "--get", "remote.origin.promisor"],
            check=False,
        )
        if partial.stdout.strip() != "true":
            return
        # Local clones do not inherit the remote's lazy-fetch configuration.
        # Repair legacy blobless caches before handing objects to a checkout.
        await self._run_remote(
            [
                "--git-dir",
                str(cache),
                "fetch",
                "--refetch",
                "--no-filter",
                "--no-tags",
                source,
                "+refs/heads/*:refs/remotes/origin/*",
            ],
            repository,
            source,
        )
        await self._runner.run(
            [
                "--git-dir",
                str(cache),
                "config",
                "--unset-all",
                "remote.origin.partialclonefilter",
            ],
            check=False,
        )
        await self._runner.run(
            ["--git-dir", str(cache), "config", "remote.origin.promisor", "false"]
        )

    async def reconstruct(self, lease: WorkspaceLease) -> WorkspaceLease:
        if lease.state in {WorkspaceLifecycle.RELEASED, WorkspaceLifecycle.RELEASING}:
            raise ValueError("Workspace já liberado; crie outra ordem de trabalho.")
        workspace = self._lease_workspace_path(lease)
        self._validate_ref(lease.repository.base_ref)
        source = self._resolve_url(lease.repository)
        self._validate_source(source, lease.repository)
        await self._validate_cache(self._cache_path(lease.repository), source)
        if urlsplit(source).scheme == "https":
            # Cache is not evidence of current access. Probe even without credentials.
            await self._run_remote(
                ["ls-remote", source, "HEAD"], lease.repository, source
            )
        if lease.state in {
            WorkspaceLifecycle.REQUESTED,
            WorkspaceLifecycle.PROVISIONING,
        }:
            await self._materialize_partial_cache(
                self._cache_path(lease.repository), lease.repository, source
            )
            # Only a journaled pre-execution checkout may be rebuilt. Approved
            # work is never discarded because of a missing directory.
            if workspace.exists():
                await asyncio.to_thread(shutil.rmtree, workspace)
            await self._runner.run(
                [
                    "clone",
                    "--no-hardlinks",
                    str(self._cache_path(lease.repository)),
                    str(workspace),
                ]
            )
            await self._runner.run(
                ["checkout", "-B", lease.branch, lease.base_sha], cwd=workspace
            )
            lease = self.transition(lease, WorkspaceLifecycle.READY)
        if not (workspace / ".git").exists():
            raise ValueError("Workspace da lease não existe.")
        head = await self._runner.run(["rev-parse", "HEAD"], cwd=workspace)
        if head.stdout.strip() != lease.base_sha and lease.state in {
            WorkspaceLifecycle.REQUESTED,
            WorkspaceLifecycle.PROVISIONING,
            WorkspaceLifecycle.READY,
        }:
            raise ValueError("Workspace não corresponde ao SHA base da lease.")
        return self.transition(lease, WorkspaceLifecycle.ACTIVE)

    async def cleanup(self, lease: WorkspaceLease) -> WorkspaceLease:
        """Remove somente o checkout da lease; cache e remotos são preservados."""
        with self.journal.exclusive(lease.workflow_id, reentrant=True):
            if lease.workflow_id in self.journal.containers():
                raise ValueError("sandbox_cleanup_pending")
            return await self._cleanup_locked(lease)

    async def _cleanup_locked(self, lease: WorkspaceLease) -> WorkspaceLease:
        workspace = self._lease_workspace_path(lease)
        now = datetime.now(timezone.utc)
        retain_until = lease.retention.retain_until
        if retain_until is not None and retain_until > now:
            return self.transition(lease, WorkspaceLifecycle.RETAINED)
        if not workspace.exists():
            return self.transition(lease, WorkspaceLifecycle.RELEASED)
        releasing = self.transition(lease, WorkspaceLifecycle.RELEASING)
        await asyncio.to_thread(shutil.rmtree, workspace)
        return self.transition(releasing, WorkspaceLifecycle.RELEASED)

    def _default_url(self, repository: RepositoryTarget) -> str:
        return f"https://{repository.scm_host}/{repository.full_name}.git"

    def _validate_source(self, source: str, repository: RepositoryTarget) -> None:
        parsed = urlsplit(source)
        if parsed.scheme == "https":
            host = (parsed.hostname or "").lower().rstrip(".")
            if host not in self._approved_hosts:
                raise ValueError("Host do repositório não está aprovado.")
            if parsed.username or parsed.password or parsed.port is not None:
                raise ValueError("URL do repositório contém autoridade insegura.")
            if (
                source != self._default_url(repository)
                or parsed.query
                or parsed.fragment
                or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository.full_name)
                is None
                or any(part in {".", ".."} for part in repository.full_name.split("/"))
            ):
                raise ValueError("URL do repositório não corresponde à ordem.")
            return
        if self._allow_local and not parsed.scheme:
            return
        raise ValueError("Repositório deve usar HTTPS em host aprovado.")

    async def _run_remote(
        self, args: list[str], repository: RepositoryTarget, source: str
    ) -> GitCommandResult:
        authentication = None
        if self._repository_access is not None and urlsplit(source).scheme == "https":
            authentication = await self._repository_access.for_repository(
                repository, source
            )
        return await self._runner.run(args, authentication=authentication)

    async def _validate_cache(self, cache: Path, source: str) -> None:
        if cache.is_symlink():
            raise ValueError("Cache Git não pode ser link simbólico.")
        if not cache.exists() or urlsplit(source).scheme != "https":
            return
        # Managed caches are standalone bare repositories, never linked worktrees.
        # Otherwise Git reads common/config while the validator inspects cache/config.
        common = cache / "commondir"
        if common.exists() or common.is_symlink():
            raise ValueError("Cache Git não pode usar diretório comum externo.")
        config = cache / "config"
        if config.is_symlink() or not config.is_file() or config.stat().st_size > 8192:
            raise ValueError("Configuração do cache Git inválida.")
        result = await self._runner.run(
            ["config", "--file", str(config), "--no-includes", "--null", "--list"]
        )
        allowed = {
            "core.repositoryformatversion",
            "core.filemode",
            "core.bare",
            "core.logallrefupdates",
            "core.ignorecase",
            "core.precomposeunicode",
            "remote.origin.url",
            "remote.origin.fetch",
            "remote.origin.tagopt",
            "remote.origin.promisor",
            "remote.origin.partialclonefilter",
            "extensions.partialclone",
            "extensions.objectformat",
        }
        origins = []
        for entry in result.stdout.split("\0"):
            if not entry:
                continue
            key, _, value = entry.partition("\n")
            if key not in allowed:
                raise ValueError("Configuração do cache Git não é permitida.")
            if key == "remote.origin.url":
                origins.append(value)
        if origins != [source] or not result.stdout.endswith("\0"):
            raise ValueError("Origem do cache Git não corresponde à ordem.")

    def _cache_path(self, repository: RepositoryTarget) -> Path:
        digest = hashlib.sha256(
            f"{repository.scm_host}/{repository.full_name}".lower().encode()
        ).hexdigest()[:24]
        return self._cache_root / f"{digest}.git"

    async def _lease_for_existing(
        self,
        workflow_id: str,
        order: WorkOrder,
        workspace: Path,
        branch: str,
        base_sha: str,
    ) -> WorkspaceLease:
        self._ensure_workspace_path(workspace)
        head = await self._runner.run(["rev-parse", "HEAD"], cwd=workspace)
        current_branch = await self._runner.run(
            ["branch", "--show-current"], cwd=workspace
        )
        if head.stdout.strip() != base_sha or current_branch.stdout.strip() != branch:
            raise ValueError("Workspace existente diverge da lease solicitada.")
        lease = WorkspaceLease(
            workflow_id=workflow_id,
            repository=order.repository,
            local_path=str(workspace),
            branch=branch,
            base_sha=base_sha,
            state=WorkspaceLifecycle.READY,
        )
        return self.transition(lease, WorkspaceLifecycle.READY)

    def _ensure_workspace_path(self, path: Path) -> None:
        if path.parent != self._workspace_root:
            raise ValueError("Workspace fora da raiz administrada.")

    def _lease_workspace_path(self, lease: WorkspaceLease) -> Path:
        self._validate_identifier(lease.workflow_id, "workflow_id")
        raw = Path(lease.local_path).expanduser()
        expected = self._workspace_root / lease.workflow_id
        if raw != expected:
            raise ValueError("Workspace fora da raiz ou ownership da lease inválido.")
        if raw.is_symlink():
            raise ValueError("Workspace da lease não pode ser um link simbólico.")
        resolved = raw.resolve()
        self._ensure_workspace_path(resolved)
        return resolved

    @classmethod
    def _validate_identifier(cls, value: str, label: str) -> None:
        if cls._SAFE_NAME.fullmatch(value) is None:
            raise ValueError(f"{label} contém caracteres inválidos.")

    @classmethod
    def _validate_ref(cls, value: str) -> None:
        if (
            cls._SAFE_REF.fullmatch(value) is None
            or ".." in value
            or "@{" in value
            or value.endswith(".lock")
        ):
            raise ValueError("base_ref inválida.")
