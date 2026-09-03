"""Execução sequencial de perfis administrados em containers descartáveis.

O daemon Docker é uma dependência privilegiada do operador. Nenhuma entrada
do repositório escolhe flags Docker, mounts, imagem, ambiente ou executável.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.factory.build_strategy import BuildProfileRegistry
from app.factory.lifecycle import WorkspaceJournal, inherited_lock_fds
from app.infrastructure.command_policy import AuthorizedBuildCommand, CommandPolicy
from app.models.build import BuildPhase, BuildProfile
from app.models.build_execution import (
    BuildOutcome,
    BuildPhaseResult,
    BuildRunResult,
    SandboxLimits,
)
from app.models.factory import BuildProfileSelection, WorkspaceLease, WorkspaceLifecycle


@dataclass(frozen=True)
class DockerOutput:
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    timed_out: bool = False


class DockerClient(Protocol):
    async def call(
        self, args: list[str], *, timeout: float, output_limit: int
    ) -> DockerOutput: ...


class _Capture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    async def read(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(8192):
            remaining = self.limit - len(self.data)
            self.data.extend(chunk[:remaining])
            self.truncated |= len(chunk) > remaining

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


class DockerCLI:
    """Cliente local com ambiente mínimo e captura limitada durante a leitura."""

    def __init__(
        self, *, executable: str = "docker", socket_path: str = "/var/run/docker.sock"
    ) -> None:
        binary = shutil.which(executable)
        if binary is None:
            raise ValueError("docker_executable_unavailable")
        if not Path(socket_path).is_absolute():
            raise ValueError("docker_socket_must_be_absolute")
        self._prefix = [binary, "--host", f"unix://{socket_path}"]

    async def call(
        self, args: list[str], *, timeout: float, output_limit: int
    ) -> DockerOutput:
        process = await asyncio.create_subprocess_exec(
            *self._prefix,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Não herda DOCKER_HOST/CONTEXT, proxies, credenciais ou env de LLM.
            env={"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C"},
            pass_fds=inherited_lock_fds(),
        )
        assert process.stdout is not None and process.stderr is not None
        stdout, stderr = _Capture(output_limit), _Capture(output_limit)
        readers = [
            asyncio.create_task(stdout.read(process.stdout)),
            asyncio.create_task(stderr.read(process.stderr)),
        ]
        timed_out = False
        try:
            async with asyncio.timeout(timeout):
                await process.wait()
                await asyncio.gather(*readers)
        except TimeoutError:
            timed_out = True
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
        return DockerOutput(
            process.returncode,
            stdout.text(),
            stderr.text(),
            stdout.truncated or stderr.truncated,
            timed_out,
        )


class BuildRunCancelled(asyncio.CancelledError):
    """Propaga cancelamento para o worker e preserva a evidência já coletada."""

    def __init__(self, report: BuildRunResult) -> None:
        super().__init__("factory_build_cancelled")
        self.report = report


class DockerBuildRunner:
    def __init__(
        self,
        registry: BuildProfileRegistry,
        docker: DockerClient,
        *,
        limits: SandboxLimits | None = None,
        allow_dependency_network: bool = False,
        redacted_values: tuple[str, ...] = (),
        journal: WorkspaceJournal | None = None,
    ) -> None:
        self._registry = registry
        self._docker = docker
        self._limits = SandboxLimits.model_validate(
            (limits or SandboxLimits()).model_dump()
        )
        self._allow_dependency_network = allow_dependency_network
        self._redacted_values = tuple(value for value in redacted_values if value)
        self._active: dict[str, str | None] = {}
        self._quarantined: dict[str, tuple[str, str]] = {}
        self._journal = journal
        if journal is not None:
            self._quarantined.update(journal.containers())

    @property
    def active_containers(self) -> dict[str, str | None]:
        return {
            **self._active,
            **{key: value[0] for key, value in self._quarantined.items()},
        }

    async def retry_cleanup(self, workflow_id: str) -> bool:
        """Libera uma quarentena somente após remoção confirmada e com ownership."""
        if workflow_id in self._active:
            return False
        if self._journal is not None:
            pending_resource = self._journal.containers().get(workflow_id)
            if pending_resource is not None:
                self._quarantined[workflow_id] = pending_resource
        pending = self._quarantined.get(workflow_id)
        if pending is None:
            return workflow_id not in self._active
        if not await self._cleanup(*pending):
            return False
        self._quarantined.pop(workflow_id, None)
        if self._journal is not None:
            self._journal.forget_container(workflow_id)
        return True

    async def run(
        self, lease: WorkspaceLease, selection: BuildProfileSelection
    ) -> BuildRunResult:
        lease = WorkspaceLease.model_validate(lease.model_dump())
        selection = BuildProfileSelection.model_validate(selection.model_dump())

        def report(
            outcome: BuildOutcome,
            phases: tuple[BuildPhaseResult, ...] = (),
            error_code: str | None = None,
        ) -> BuildRunResult:
            return BuildRunResult(
                profile_name=selection.selected_profile,
                profile_digest=selection.profile_digest,
                outcome=outcome,
                phases=phases,
                error_code=error_code,
            )

        if (
            self._journal is not None
            and lease.workflow_id in self._journal.containers()
        ):
            self._quarantined[lease.workflow_id] = self._journal.containers()[
                lease.workflow_id
            ]
        if lease.workflow_id in self._quarantined:
            return report(
                BuildOutcome.INFRASTRUCTURE_ERROR, error_code="sandbox_cleanup_pending"
            )
        if lease.workflow_id in self._active:
            return report(BuildOutcome.INFRASTRUCTURE_ERROR, error_code="workflow_busy")
        if lease.state not in {WorkspaceLifecycle.READY, WorkspaceLifecycle.ACTIVE}:
            return report(BuildOutcome.POLICY_REJECTION, error_code="lease_not_active")
        try:
            profile = self._registry.profile_for(selection)
        except ValueError:
            return report(BuildOutcome.POLICY_REJECTION, error_code="profile_drift")
        self._active[lease.workflow_id] = None
        results: list[BuildPhaseResult] = []
        try:
            for phase in profile.phases:
                if self._journal is not None:
                    self._journal.record_phase(
                        lease.workflow_id, phase.name.value, "running"
                    )
                result = await self._run_phase(lease, profile, phase)
                if self._journal is not None:
                    self._journal.record_phase(
                        lease.workflow_id, phase.name.value, result.outcome.value
                    )
                results.append(result)
                if result.outcome == BuildOutcome.CANCELLED:
                    raise BuildRunCancelled(report(result.outcome, tuple(results)))
                if result.outcome != BuildOutcome.SUCCESS:
                    return report(result.outcome, tuple(results), result.error_code)
            return report(BuildOutcome.SUCCESS, tuple(results))
        except BuildRunCancelled:
            raise
        except asyncio.CancelledError as exc:
            raise BuildRunCancelled(
                report(BuildOutcome.CANCELLED, tuple(results))
            ) from exc
        finally:
            self._active.pop(lease.workflow_id, None)

    def _create_args(
        self, command: AuthorizedBuildCommand, root: Path, name: str, token: str
    ) -> list[str]:
        # Docker --mount usa CSV; não admite delimitadores vindos do path.
        if any(char in str(root) for char in ',"\n\r'):
            raise ValueError("workspace_mount_path_invalid")
        cwd = command.cwd.relative_to(root).as_posix()
        limits = self._limits
        memory = f"{limits.memory_mib}m"
        return [
            "create",
            "--name",
            name,
            "--label",
            f"forgehand.run_token={token}",
            "--pull",
            "never",
            "--init",
            "--no-healthcheck",
            "--network",
            "bridge" if command.network_enabled else "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--cpus",
            str(limits.cpus),
            "--pids-limit",
            str(limits.pids),
            "--ulimit",
            "core=0",
            "--ulimit",
            "nofile=1024:1024",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmp_mib}m",
            "--shm-size",
            "16m",
            "--log-driver",
            "none",
            "--user",
            f"{os.getuid() or 65534}:{os.getgid() or 65534}",
            "--mount",
            f"type=bind,src={root},dst=/workspace",
            "--workdir",
            "/workspace" if cwd == "." else f"/workspace/{cwd}",
            # env -i ignora o ENV da imagem; ENTRYPOINT/CMD não podem trocar argv.
            "--entrypoint",
            "/usr/bin/env",
            command.image,
            "-i",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "HOME=/tmp",
            *(f"{key}={value}" for key, value in command.environment),
            *command.argv,
        ]

    async def _control(self, args: list[str]) -> DockerOutput:
        return await self._docker.call(
            args, timeout=self._limits.control_timeout_seconds, output_limit=4096
        )

    async def _cleanup(
        self, name: str, token: str, *, absence_is_success: bool = True
    ) -> bool:
        try:
            owned = await self._control(
                ["inspect", "--format", "{{json .Config.Labels}}", name]
            )
            if owned.timed_out:
                return False
            if owned.exit_code != 0:
                return absence_is_success and any(
                    message in owned.stderr
                    for message in ("No such object:", "No such container:")
                )
            labels = json.loads(owned.stdout)
            if (
                not isinstance(labels, dict)
                or labels.get("forgehand.run_token") != token
            ):
                return False
            removed = await self._control(["rm", "--force", "--volumes", name])
            return removed.exit_code == 0 and not removed.timed_out
        except (OSError, ValueError):
            return False

    def _sanitize(self, value: str, limit: int) -> str:
        value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
        value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value)
        for secret in self._redacted_values:
            value = value.replace(secret, "[REDACTED]")
        return value[:limit]

    async def _run_phase(
        self, lease: WorkspaceLease, profile: BuildProfile, phase: BuildPhase
    ) -> BuildPhaseResult:
        started = time.monotonic()
        outcome = BuildOutcome.INFRASTRUCTURE_ERROR
        error_code: str | None = None
        output = DockerOutput(None)
        exit_code: int | None = None
        network_enabled = False
        cancelled = False
        cleanup_failed = False
        token = uuid4().hex
        name = f"forgehand-{token}"
        create_attempted = False
        creation_uncertain = False
        try:
            try:
                command = CommandPolicy(profile=profile).validate_phase(
                    phase.name,
                    Path(lease.local_path),
                    allow_dependency_network=self._allow_dependency_network,
                )
                root = Path(lease.local_path).resolve()
                args = self._create_args(command, root, name, token)
            except ValueError:
                outcome = BuildOutcome.POLICY_REJECTION
                error_code = (
                    "dependency_preparation_not_authorized"
                    if phase.network == "dependencies"
                    and not self._allow_dependency_network
                    else "phase_policy_rejected"
                )
            else:
                network_enabled = command.network_enabled
                image = await self._control(
                    [
                        "image",
                        "inspect",
                        "--format",
                        "{{json .Config.Volumes}}",
                        profile.image,
                    ]
                )
                if image.exit_code != 0 or image.timed_out:
                    error_code = "sandbox_image_unavailable"
                elif json.loads(image.stdout) not in (None, {}):
                    outcome = BuildOutcome.POLICY_REJECTION
                    error_code = "image_declares_unapproved_volumes"
                else:
                    create_attempted = True
                    creation_uncertain = True
                    self._active[lease.workflow_id] = name
                    if self._journal is not None:
                        self._journal.record_container(lease.workflow_id, name, token)
                    created = await self._control(args)
                    creation_uncertain = created.timed_out
                    if created.exit_code != 0 or created.timed_out:
                        error_code = "sandbox_create_failed"
                    else:
                        output = await self._docker.call(
                            ["start", "--attach", name],
                            timeout=command.timeout_seconds,
                            output_limit=command.output_limit,
                        )
                        if output.timed_out:
                            outcome = BuildOutcome.TIMEOUT
                            error_code = "phase_timeout"
                        else:
                            inspected = await self._control(
                                ["inspect", "--format", "{{json .State}}", name]
                            )
                            if inspected.exit_code != 0 or inspected.timed_out:
                                error_code = "sandbox_state_unavailable"
                            else:
                                state = json.loads(inspected.stdout)
                                if (
                                    not isinstance(state, dict)
                                    or state.get("Running") is not False
                                    or type(state.get("ExitCode")) is not int
                                    or state.get("Status") != "exited"
                                ):
                                    error_code = "sandbox_state_invalid"
                                else:
                                    exit_code = state["ExitCode"]
                                    if state.get("OOMKilled") is True:
                                        outcome = BuildOutcome.RESOURCE_LIMIT
                                        error_code = "memory_limit_exceeded"
                                    elif exit_code != 0:
                                        outcome = BuildOutcome.COMMAND_FAILURE
                                    elif output.exit_code != 0:
                                        error_code = "sandbox_attach_failed"
                                    else:
                                        outcome = BuildOutcome.SUCCESS
        except asyncio.CancelledError:
            cancelled = True
        except (OSError, ValueError):
            error_code = "sandbox_infrastructure_error"
        finally:
            if create_attempted:
                # Cancelar o cliente durante create não prova que o daemon
                # cancelou o pedido. Ausência momentânea exige intervenção.
                cleanup = asyncio.create_task(
                    self._cleanup(
                        name, token, absence_is_success=not creation_uncertain
                    )
                )
                # Uma segunda solicitação de cancelamento não abandona a remoção.
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup_failed = not cleanup.result()
                if cleanup_failed:
                    self._quarantined[lease.workflow_id] = (name, token)
                elif self._journal is not None:
                    self._journal.forget_container(lease.workflow_id)
                self._active[lease.workflow_id] = None
        if cleanup_failed:
            outcome = BuildOutcome.INFRASTRUCTURE_ERROR
            error_code = "sandbox_cleanup_failed"
        if cancelled:
            outcome = BuildOutcome.CANCELLED
            if not cleanup_failed:
                error_code = "phase_cancelled"
        return BuildPhaseResult(
            phase=phase.name,
            outcome=outcome,
            command=phase.argv,
            image=profile.image,
            cwd=phase.cwd,
            duration_seconds=time.monotonic() - started,
            exit_code=exit_code,
            stdout=self._sanitize(output.stdout, phase.output_limit),
            stderr=self._sanitize(output.stderr, phase.output_limit),
            output_truncated=output.truncated,
            network_enabled=network_enabled,
            cleanup_failed=cleanup_failed,
            container_name=name if create_attempted else None,
            error_code=error_code,
        )
