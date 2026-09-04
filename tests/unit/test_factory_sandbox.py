import asyncio
import json
import sys
from pathlib import Path

import pytest

from app.factory.build_strategy import BuildProfileRegistry
from app.factory.sandbox import (
    BuildRunCancelled,
    DockerBuildRunner,
    DockerCLI,
    DockerOutput,
)
from app.models.build import BuildPhase, BuildProfile
from app.models.build_execution import BuildOutcome, BuildRunResult, SandboxLimits
from app.models.factory import (
    BuildProfileSelection,
    RepositoryTarget,
    WorkspaceLease,
    WorkspaceLifecycle,
)


def make_profile(*phases: dict) -> BuildProfile:
    return BuildProfile(
        name="python-tests",
        ecosystem="python",
        image="python@sha256:" + "a" * 64,
        phases=tuple(BuildPhase.model_validate(phase) for phase in phases)
        or (BuildPhase(name="test", argv=("/usr/local/bin/python", "-m", "pytest")),),
    )


def make_lease(root: Path) -> WorkspaceLease:
    return WorkspaceLease(
        workflow_id="sandbox-test",
        repository=RepositoryTarget(full_name="acme/api"),
        local_path=str(root),
        base_sha="a" * 40,
        branch="forgehand/sandbox-test",
        state=WorkspaceLifecycle.READY,
    )


def selection(profile: BuildProfile) -> BuildProfileSelection:
    return BuildProfileSelection(
        selected_profile=profile.name,
        selection_reason="explicit",
        phases=[phase.name.value for phase in profile.phases],
        profile_digest=profile.fingerprint(),
        architecture_digest=profile.architecture.fingerprint()
        if profile.architecture
        else None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_bad,generated_bad", [(True, False), (False, True), (False, False)]
)
async def test_architecture_gate_before_and_after_build(
    tmp_path, initial_bad, generated_bad
):
    from app.models.architecture import ArchitecturePolicy

    policy = ArchitecturePolicy(
        rules=[
            {
                "id": "domain",
                "source": "domain",
                "forbidden": ["requests"],
                "remediation": "Use uma interface de domínio no lugar do cliente HTTP.",
            }
        ]
    )
    profile = make_profile().model_copy(update={"architecture": policy})
    target = tmp_path / "domain.py"
    target.write_text("import requests" if initial_bad else "import os")

    class GeneratingDocker(FakeDocker):
        async def call(self, args, **kwargs):
            result = await super().call(args, **kwargs)
            if args[0] == "start" and generated_bad:
                target.write_text("import requests")
            return result

    docker = GeneratingDocker()
    runner = DockerBuildRunner(BuildProfileRegistry({profile.name: profile}), docker)
    report = await runner.run(make_lease(tmp_path), selection(profile))
    assert report.architecture is not None
    assert report.architecture.passed == (not initial_bad and not generated_bad)
    assert (report.outcome == BuildOutcome.SUCCESS) == report.architecture.passed
    assert not runner.active_containers
    if initial_bad:
        assert not docker.calls and not report.phases
    if generated_bad:
        assert report.phases[0].outcome == BuildOutcome.SUCCESS
        assert report.error_code == "architecture_policy_failed"
    assert BuildRunResult.model_validate(report.model_dump()) == report


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.token = ""
        self.exit_code = 0
        self.oom = False
        self.stdout = "passed"
        self.start_result = None
        self.image_result = DockerOutput(0, "null")
        self.create_result = DockerOutput(0, "a" * 64)
        self.rm_result = DockerOutput(0)
        self.state_result = None
        self.wrong_owner = False

    async def call(self, args, *, timeout, output_limit):
        self.calls.append((args, timeout, output_limit))
        if args[0] == "image":
            return self.image_result
        if args[0] == "create":
            self.token = args[args.index("--label") + 1].split("=", 1)[1]
            return self.create_result
        if args[0] == "start":
            return self.start_result or DockerOutput(self.exit_code, self.stdout)
        if args[0] == "inspect":
            if ".Config.Labels" in args[2]:
                return DockerOutput(
                    0,
                    json.dumps(
                        {
                            "forgehand.run_token": "foreign"
                            if self.wrong_owner
                            else self.token
                        }
                    ),
                )
            return self.state_result or DockerOutput(
                0,
                json.dumps(
                    {
                        "Status": "exited",
                        "Running": False,
                        "ExitCode": self.exit_code,
                        "OOMKilled": self.oom,
                    }
                ),
            )
        if args[0] == "rm":
            return self.rm_result
        raise AssertionError(args)


def runner(profile, docker, **kwargs):
    return DockerBuildRunner(
        BuildProfileRegistry({profile.name: profile}), docker, **kwargs
    )


@pytest.mark.asyncio
async def test_sandbox_uses_exact_profile_and_minimal_mounts(tmp_path):
    profile = make_profile(
        {
            "name": "test",
            "argv": ["/usr/local/bin/python", "-m", "pytest"],
            "environment": {"CI": "true"},
            "timeout_seconds": 31,
            "output_limit": 1024,
        }
    )
    docker = FakeDocker()
    execution = runner(profile, docker)
    result = await execution.run(make_lease(tmp_path), selection(profile))

    assert result.outcome == BuildOutcome.SUCCESS
    args = next(args for args, _, _ in docker.calls if args[0] == "create")
    for flag in ("--read-only", "--init", "--no-healthcheck"):
        assert flag in args
    for flag, value in {
        "--network": "none",
        "--cap-drop": "ALL",
        "--security-opt": "no-new-privileges",
        "--pull": "never",
        "--memory": "512m",
        "--memory-swap": "512m",
        "--cpus": "1.0",
        "--pids-limit": "128",
        "--entrypoint": "/usr/bin/env",
        "--log-driver": "none",
        "--workdir": "/workspace",
    }.items():
        assert args[args.index(flag) + 1] == value
    assert args.count("--mount") == 1
    assert args[args.index("--mount") + 1] == f"type=bind,src={tmp_path},dst=/workspace"
    assert args[args.index(profile.image) + 1 :] == [
        "-i",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "HOME=/tmp",
        "CI=true",
        "/usr/local/bin/python",
        "-m",
        "pytest",
    ]
    assert "sh" not in args and "--env-file" not in args
    assert next(
        (timeout, limit) for a, timeout, limit in docker.calls if a[0] == "start"
    ) == (31, 1024)
    assert docker.calls[-1][0][:3] == ["rm", "--force", "--volumes"]
    assert execution.active_containers == {}
    assert BuildRunResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exit_code,oom,expected",
    [
        (0, False, BuildOutcome.SUCCESS),
        (1, False, BuildOutcome.COMMAND_FAILURE),
        (125, False, BuildOutcome.COMMAND_FAILURE),
        (137, False, BuildOutcome.COMMAND_FAILURE),
        (137, True, BuildOutcome.RESOURCE_LIMIT),
    ],
)
async def test_exit_code_137_alone_is_not_misreported_as_oom(
    tmp_path, exit_code, oom, expected
):
    profile, docker = make_profile(), FakeDocker()
    docker.exit_code, docker.oom = exit_code, oom
    result = await runner(profile, docker).run(make_lease(tmp_path), selection(profile))
    assert result.outcome == expected
    assert result.phases[0].exit_code == exit_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,error",
    [
        ("create", "sandbox_create_failed"),
        ("state", "sandbox_state_unavailable"),
        ("image", "sandbox_image_unavailable"),
        ("cleanup", "sandbox_cleanup_failed"),
        ("attach", "sandbox_attach_failed"),
        ("malformed", "sandbox_infrastructure_error"),
        ("running", "sandbox_state_invalid"),
    ],
)
async def test_infrastructure_failures_are_typed(tmp_path, mode, error):
    profile, docker = make_profile(), FakeDocker()
    if mode == "create":
        docker.create_result = DockerOutput(125)
    if mode == "image":
        docker.image_result = DockerOutput(1)
    if mode == "state":
        docker.state_result = DockerOutput(1)
    if mode == "cleanup":
        docker.rm_result = DockerOutput(1)
    if mode == "attach":
        docker.start_result = DockerOutput(125)
    if mode == "malformed":
        docker.state_result = DockerOutput(0, "invalid")
    if mode == "running":
        docker.state_result = DockerOutput(0, '{"Running":true}')
    result = await runner(profile, docker).run(make_lease(tmp_path), selection(profile))
    assert result.outcome == BuildOutcome.INFRASTRUCTURE_ERROR
    assert result.error_code == error


@pytest.mark.asyncio
async def test_timeout_removes_container_and_stops_remaining_phases(tmp_path):
    profile = make_profile(
        {"name": "build", "argv": ["/usr/local/bin/python", "build.py"]},
        {"name": "test", "argv": ["/usr/local/bin/pytest"]},
    )
    docker = FakeDocker()
    docker.start_result = DockerOutput(-9, "partial", timed_out=True)
    result = await runner(profile, docker).run(make_lease(tmp_path), selection(profile))
    assert result.outcome == BuildOutcome.TIMEOUT
    assert len(result.phases) == 1
    assert result.phases[0].stdout == "partial"
    assert docker.calls[-1][0][0] == "rm"


@pytest.mark.asyncio
async def test_foreign_container_is_never_removed(tmp_path):
    profile, docker = make_profile(), FakeDocker()
    docker.wrong_owner = True
    result = await runner(profile, docker).run(make_lease(tmp_path), selection(profile))
    assert result.error_code == "sandbox_cleanup_failed"
    assert result.phases[0].cleanup_failed
    assert not any(args[0] == "rm" for args, _, _ in docker.calls)


@pytest.mark.asyncio
async def test_cleanup_failure_quarantines_workflow_until_confirmed_removal(tmp_path):
    profile, docker = make_profile(), FakeDocker()
    docker.rm_result = DockerOutput(1)
    execution = runner(profile, docker)
    active_lease = make_lease(tmp_path)
    first = await execution.run(active_lease, selection(profile))
    assert first.phases[0].cleanup_failed
    assert (
        execution.active_containers[active_lease.workflow_id]
        == first.phases[0].container_name
    )
    second = await execution.run(active_lease, selection(profile))
    assert second.error_code == "sandbox_cleanup_pending"
    assert not await execution.retry_cleanup(active_lease.workflow_id)
    docker.rm_result = DockerOutput(0)
    assert await execution.retry_cleanup(active_lease.workflow_id)
    assert execution.active_containers == {}
    assert (
        await execution.run(active_lease, selection(profile))
    ).outcome == BuildOutcome.SUCCESS


@pytest.mark.asyncio
async def test_create_timeout_with_absent_container_is_not_claimed_clean(tmp_path):
    class UncertainDocker(FakeDocker):
        async def call(self, args, *, timeout, output_limit):
            if args[0] == "create":
                return DockerOutput(-9, timed_out=True)
            if args[0] == "inspect":
                return DockerOutput(1, stderr="No such object: pending-container")
            return await super().call(args, timeout=timeout, output_limit=output_limit)

    profile, docker = make_profile(), UncertainDocker()
    execution = runner(profile, docker)
    result = await execution.run(make_lease(tmp_path), selection(profile))
    assert result.error_code == "sandbox_cleanup_failed"
    assert result.phases[0].cleanup_failed
    assert execution.active_containers


@pytest.mark.asyncio
async def test_successful_phases_preserve_operator_order(tmp_path):
    profile = make_profile(
        *(
            {"name": name, "argv": ["/usr/local/bin/python", "probe.py"]}
            for name in ("prepare", "build", "lint", "types", "test")
        )
    )
    docker = FakeDocker()
    result = await runner(profile, docker).run(make_lease(tmp_path), selection(profile))
    assert result.outcome == BuildOutcome.SUCCESS
    assert [phase.phase.value for phase in result.phases] == [
        "prepare",
        "build",
        "lint",
        "types",
        "test",
    ]
    operations = [args[0] for args, _, _ in docker.calls]
    assert operations == ["image", "create", "start", "inspect", "inspect", "rm"] * 5


@pytest.mark.asyncio
async def test_later_phase_revalidates_paths_after_preparation(tmp_path):
    profile = make_profile(
        {"name": "prepare", "argv": ["/usr/local/bin/python", "prepare.py"]},
        {"name": "test", "argv": ["/usr/local/bin/pytest", "tests"]},
    )

    class SymlinkDocker(FakeDocker):
        async def call(self, args, *, timeout, output_limit):
            if args[0] == "start":
                (tmp_path / "tests").symlink_to(
                    tmp_path.parent, target_is_directory=True
                )
            return await super().call(args, timeout=timeout, output_limit=output_limit)

    docker = SymlinkDocker()
    result = await runner(profile, docker).run(make_lease(tmp_path), selection(profile))
    assert result.phases[0].outcome == BuildOutcome.SUCCESS
    assert result.phases[1].outcome == BuildOutcome.POLICY_REJECTION
    assert sum(args[0] == "start" for args, _, _ in docker.calls) == 1


@pytest.mark.asyncio
async def test_dependency_network_requires_explicit_operator_grant(tmp_path):
    profile = make_profile(
        {
            "name": "prepare",
            "argv": ["/usr/local/bin/uv", "sync"],
            "network": "dependencies",
        },
        {"name": "test", "argv": ["/usr/local/bin/pytest"]},
    )
    denied = FakeDocker()
    result = await runner(profile, denied).run(make_lease(tmp_path), selection(profile))
    assert result.outcome == BuildOutcome.POLICY_REJECTION
    assert result.error_code == "dependency_preparation_not_authorized"
    assert denied.calls == []
    allowed = FakeDocker()
    result = await runner(profile, allowed, allow_dependency_network=True).run(
        make_lease(tmp_path), selection(profile)
    )
    assert result.outcome == BuildOutcome.SUCCESS
    creates = [args for args, _, _ in allowed.calls if args[0] == "create"]
    assert [args[args.index("--network") + 1] for args in creates] == ["bridge", "none"]
    assert [phase.phase.value for phase in result.phases] == ["prepare", "test"]


@pytest.mark.asyncio
async def test_declared_image_volumes_are_rejected(tmp_path):
    profile, docker = make_profile(), FakeDocker()
    docker.image_result = DockerOutput(0, '{"/extra":{}}')
    result = await runner(profile, docker).run(make_lease(tmp_path), selection(profile))
    assert result.outcome == BuildOutcome.POLICY_REJECTION
    assert result.error_code == "image_declares_unapproved_volumes"
    assert len(docker.calls) == 1


@pytest.mark.asyncio
async def test_profile_drift_rejected_before_docker(tmp_path):
    profile, docker = make_profile(), FakeDocker()
    drifted = selection(profile).model_copy(update={"profile_digest": "b" * 64})
    result = await runner(profile, docker).run(make_lease(tmp_path), drifted)
    assert result.error_code == "profile_drift"
    assert docker.calls == []


@pytest.mark.asyncio
async def test_symlink_or_inactive_lease_never_executes(tmp_path):
    profile, docker = make_profile(), FakeDocker()
    execution = runner(profile, docker)
    inactive = make_lease(tmp_path).model_copy(
        update={"state": WorkspaceLifecycle.RELEASED}
    )
    assert (
        await execution.run(inactive, selection(profile))
    ).error_code == "lease_not_active"
    linked = tmp_path / "link"
    linked.symlink_to(tmp_path, target_is_directory=True)
    result = await execution.run(make_lease(linked), selection(profile))
    assert result.outcome == BuildOutcome.POLICY_REJECTION
    assert docker.calls == []


@pytest.mark.asyncio
async def test_evidence_is_sanitized_and_redacted(tmp_path):
    profile, docker = make_profile(), FakeDocker()
    docker.stdout = "\x1b[31msecret-value\x1b[0m\x00 passed"
    result = await runner(profile, docker, redacted_values=("secret-value",)).run(
        make_lease(tmp_path), selection(profile)
    )
    assert result.phases[0].stdout == "[REDACTED] passed"


@pytest.mark.asyncio
async def test_cancellation_waits_for_owned_container_removal(tmp_path):
    started, removing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class BlockingDocker(FakeDocker):
        async def call(self, args, *, timeout, output_limit):
            if args[0] == "start":
                started.set()
                await asyncio.Event().wait()
            if args[0] == "rm":
                removing.set()
                await release.wait()
            return await super().call(args, timeout=timeout, output_limit=output_limit)

    profile, docker = make_profile(), BlockingDocker()
    execution = runner(profile, docker)
    active_lease = make_lease(tmp_path)
    task = asyncio.create_task(execution.run(active_lease, selection(profile)))
    await asyncio.wait_for(started.wait(), 1)
    assert execution.active_containers[active_lease.workflow_id].startswith(
        "forgehand-"
    )
    duplicate = await execution.run(active_lease, selection(profile))
    assert duplicate.error_code == "workflow_busy"
    task.cancel()
    await asyncio.wait_for(removing.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(BuildRunCancelled) as cancelled:
        await asyncio.wait_for(task, 1)
    assert cancelled.value.report.outcome == BuildOutcome.CANCELLED
    assert not cancelled.value.report.phases[0].cleanup_failed
    assert execution.active_containers == {}


@pytest.mark.asyncio
async def test_cli_bounds_capture_and_does_not_inherit_credentials(monkeypatch):
    original = asyncio.create_subprocess_exec
    received = {}

    async def substitute(*args, **kwargs):
        received.update(kwargs)
        return await original(sys.executable, "-c", "print('x' * 100000)", **kwargs)

    monkeypatch.setenv("OPENAI_API_KEY", "do-not-inherit")
    monkeypatch.setenv("GITHUB_TOKEN", "do-not-inherit")
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted:1234")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", substitute)
    result = await DockerCLI(executable=sys.executable).call(
        [], timeout=2, output_limit=1024
    )
    assert result.exit_code == 0
    assert result.truncated and len(result.stdout) == 1024
    assert set(received["env"]) == {"PATH", "HOME", "LANG"}


@pytest.mark.asyncio
async def test_cli_timeout_kills_its_child(monkeypatch):
    original = asyncio.create_subprocess_exec
    children = []

    async def substitute(*args, **kwargs):
        child = await original(
            sys.executable, "-c", "import time; time.sleep(30)", **kwargs
        )
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", substitute)
    result = await DockerCLI(executable=sys.executable).call(
        [], timeout=0.02, output_limit=1024
    )
    assert result.timed_out
    assert children[0].returncode is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"memory_mib": 0},
        {"cpus": float("nan")},
        {"pids": 0},
        {"control_timeout_seconds": 0},
        {"tmp_mib": 1000000},
    ],
)
def test_resource_limits_cannot_be_disabled(overrides):
    with pytest.raises(ValueError):
        SandboxLimits(**overrides)
