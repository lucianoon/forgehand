import os
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.infrastructure.command_policy import CommandPolicy
from app.models.build import BuildPhase, BuildPhaseName, BuildProfile
from app.models.factory import WorkspaceRetention

# Factory mode é POSIX por design (lock fcntl, dir_fd/O_NOFOLLOW, grupo de
# processos, caminhos de lease em /): no Windows só o mission control roda.
pytestmark = pytest.mark.skipif(os.name != "posix", reason="factory mode exige POSIX")


def profile(*phases: BuildPhase) -> BuildProfile:
    return BuildProfile(
        name="python-tests",
        ecosystem="python",
        image="python@sha256:" + "a" * 64,
        phases=phases
        or (
            BuildPhase(
                name=BuildPhaseName.TEST,
                argv=("/usr/local/bin/python", "-m", "pytest", "tests"),
                environment={"CI": "true"},
            ),
        ),
    )


@pytest.mark.parametrize(
    "argv",
    [
        (),
        ("python", "-m", "pytest"),
        ("/usr/bin/sh", "-c", "pytest"),
        ("/workspace/bin/python", "-m", "pytest"),
        ("//workspace/bin/python", "-m", "pytest"),
        ("/usr/../bin/python", "-m", "pytest"),
        ("/usr//bin/python", "-m", "pytest"),
        ("/usr/bin/python", "tests;whoami"),
        ("/usr/bin/python", "tests&&whoami"),
        ("/usr/bin/python", "tests|whoami"),
        ("/usr/bin/python", "$(whoami)"),
        ("/usr/bin/python", "`whoami`"),
        ("/usr/bin/python", "tests>output"),
        ("/usr/bin/python", "tests\nwhoami"),
        ("/usr/bin/python", "tests\x00"),
        ("/usr/bin/python", "../outside.py"),
        ("/usr/bin/python", "/tmp/outside.py"),
        ("/usr/bin/python", "~/outside.py"),
        ("/usr/bin/python", "--config=../outside.toml"),
        ("/usr/bin/python", "--config=/tmp/outside.toml"),
        ("/usr/bin/python", "-I/tmp"),
        ("/usr/bin/python", "-I../outside"),
        ("/usr/bin/pytest", "--override-ini=cache_dir=cache"),
        ("/usr/bin/pytest", "-o", "cache_dir=cache"),
        ("/usr/bin/python", "https://example.test/code.py"),
        ("/usr/bin/python", ""),
        ("/usr/bin/python", "x" * 4097),
    ],
)
def test_build_phase_rejects_unapproved_command_syntax(argv: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        BuildPhase(name=BuildPhaseName.TEST, argv=argv)


@pytest.mark.parametrize(
    "cwd", ["", "..", "../other", "/tmp", "a/../b", "a//b", "./a", "a;pwd"]
)
def test_build_phase_rejects_unconfined_cwd(cwd: str) -> None:
    with pytest.raises(ValidationError, match="cwd"):
        BuildPhase(name=BuildPhaseName.TEST, argv=("/usr/bin/pytest",), cwd=cwd)


@pytest.mark.parametrize(
    "key",
    ["OPENAI_API_KEY", "GITHUB_TOKEN", "PATH", "PYTHONPATH", "NODE_OPTIONS", "HOME"],
)
def test_build_phase_rejects_secret_and_runtime_environment_keys(key: str) -> None:
    with pytest.raises(ValidationError, match="ambiente"):
        BuildPhase(
            name=BuildPhaseName.TEST,
            argv=("/usr/bin/pytest",),
            environment={key: "untrusted"},
        )


@pytest.mark.parametrize("value", ["$(secret)", "a\nb", "a;b"])
def test_environment_values_cannot_contain_shell_syntax(value: str) -> None:
    with pytest.raises(ValidationError, match="sintaxe shell"):
        BuildPhase(
            name=BuildPhaseName.TEST,
            argv=("/usr/bin/pytest",),
            environment={"CI": value},
        )


@pytest.mark.parametrize("image", ["python:3.12", "python:latest", "python@sha256:abc"])
def test_build_profile_requires_digest_pinned_image(image: str) -> None:
    with pytest.raises(ValidationError):
        BuildProfile.model_validate({**profile().model_dump(), "image": image})


def test_build_profile_rejects_duplicate_phases_and_late_preparation() -> None:
    test = BuildPhase(name=BuildPhaseName.TEST, argv=("/usr/bin/pytest",))
    prepare = BuildPhase(name=BuildPhaseName.PREPARE, argv=("/usr/bin/uv", "sync"))
    with pytest.raises(ValidationError, match="repetir"):
        profile(test, test)
    with pytest.raises(ValidationError, match="primeira"):
        profile(test, prepare)


def test_only_preparation_can_request_network() -> None:
    with pytest.raises(ValidationError, match="Somente prepare"):
        BuildPhase(
            name=BuildPhaseName.TEST, argv=("/usr/bin/pytest",), network="dependencies"
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": 3601},
        {"output_limit": 255},
        {"output_limit": 100001},
        {"shell": True},
        {"argv": ["/usr/bin/pytest"] * 65},
    ],
)
def test_build_phase_rejects_unknown_options_and_unbounded_limits(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        BuildPhase.model_validate(
            {"name": "test", "argv": ["/usr/bin/pytest"], **overrides}
        )


def test_profile_fingerprint_covers_entire_contract_and_is_order_independent() -> None:
    original = profile()
    reordered = dict(reversed(list(original.model_dump().items())))
    assert (
        BuildProfile.model_validate(reordered).fingerprint() == original.fingerprint()
    )
    changed = original.model_dump()
    changed["phases"][0]["timeout_seconds"] = 121
    assert BuildProfile.model_validate(changed).fingerprint() != original.fingerprint()
    changed = original.model_dump()
    changed["phases"][0]["environment"]["CI"] = "false"
    assert BuildProfile.model_validate(changed).fingerprint() != original.fingerprint()


def test_authorization_carries_exact_immutable_contract(tmp_path: Path) -> None:
    selected = profile()
    command = CommandPolicy(profile=selected).validate_phase("test", tmp_path)

    assert command.profile_name == selected.name
    assert command.profile_digest == selected.fingerprint()
    assert command.phase is BuildPhaseName.TEST
    assert command.image == selected.image
    assert command.argv == selected.phases[0].argv
    assert command.cwd == tmp_path.resolve()
    assert command.environment == (("CI", "true"),)
    assert command.timeout_seconds == 120
    assert command.output_limit == 12000
    assert command.network_enabled is False
    with pytest.raises(FrozenInstanceError):
        setattr(command, "network_enabled", True)


@pytest.mark.parametrize(
    "argv",
    [
        ("/tmp/python", "-m", "pytest", "tests"),
        ("/usr/local/bin/python", "-c", "print('unapproved')"),
        ("/usr/local/bin/python", "-m", "pytest", "tests;whoami"),
        ("/usr/local/bin/python", "-m", "pytest", "../other"),
        ("/usr/local/bin/python", "-m", "pytest"),
    ],
)
def test_agent_cannot_override_approved_argv(
    tmp_path: Path, argv: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError, match="argumentos divergem"):
        CommandPolicy(profile=profile()).validate_phase("test", tmp_path, argv=argv)


@pytest.mark.parametrize(
    "environment", [{}, {"CI": "false"}, {"CI": "true", "GITHUB_TOKEN": "secret"}]
)
def test_agent_cannot_override_environment(
    tmp_path: Path, environment: dict[str, str]
) -> None:
    with pytest.raises(ValueError, match="Ambiente diverge"):
        CommandPolicy(profile=profile()).validate_phase(
            "test", tmp_path, environment=environment
        )


def test_agent_cannot_override_cwd(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cwd diverge"):
        CommandPolicy(profile=profile()).validate_phase("test", tmp_path, cwd="tests")


def test_authorization_rejects_unselected_or_missing_phase(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Nenhum perfil"):
        CommandPolicy().validate_phase("test", tmp_path)
    with pytest.raises(ValueError, match="Fase não existe"):
        CommandPolicy(profile=profile()).validate_phase("build", tmp_path)
    with pytest.raises(ValueError, match="fase completa"):
        CommandPolicy(profile=profile()).parse("python -m pytest")


def test_legacy_policy_parse_stays_compatible() -> None:
    assert CommandPolicy().parse('python -m pytest "tests/unit"') == [
        "python",
        "-m",
        "pytest",
        "tests/unit",
    ]
    with pytest.raises(ValueError, match="Executável não permitido"):
        CommandPolicy().parse("curl https://example.test")
    with pytest.raises(ValueError, match="vazio"):
        CommandPolicy().parse("")


def test_policy_copies_and_revalidates_operator_profile(tmp_path: Path) -> None:
    original = profile()
    policy = CommandPolicy(profile=original)
    original.phases[0].environment["GITHUB_TOKEN"] = "injected"
    assert policy.validate_phase("test", tmp_path).environment == (("CI", "true"),)
    with pytest.raises(ValidationError, match="ambiente"):
        CommandPolicy(profile=original)


def test_host_environment_is_never_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "host-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "host-secret")
    assert CommandPolicy(profile=profile()).validate_phase(
        "test", tmp_path
    ).environment == (("CI", "true"),)


def test_preparation_needs_explicit_network_authorization(tmp_path: Path) -> None:
    phase = BuildPhase(
        name=BuildPhaseName.PREPARE,
        argv=("/usr/bin/uv", "sync"),
        network="dependencies",
    )
    policy = CommandPolicy(profile=profile(phase))
    with pytest.raises(ValueError, match="dependency_preparation_not_authorized"):
        policy.validate_phase("prepare", tmp_path)
    assert (
        policy.validate_phase(
            "prepare", tmp_path, allow_dependency_network=True
        ).network_enabled
        is True
    )


def test_network_grant_does_not_enable_it_for_offline_phase(tmp_path: Path) -> None:
    assert (
        CommandPolicy(profile=profile())
        .validate_phase("test", tmp_path, allow_dependency_network=True)
        .network_enabled
        is False
    )


def test_policy_rejects_relative_missing_global_or_symlink_root(tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    policy = CommandPolicy(profile=profile())
    for root in (Path("relative"), tmp_path / "missing", Path("/"), alias):
        with pytest.raises(ValueError, match="Raiz"):
            policy.validate_phase("test", root)


def test_policy_confines_existing_cwd(tmp_path: Path) -> None:
    lease = tmp_path / "lease"
    lease.mkdir()
    (lease / "subproject").mkdir()
    phase = BuildPhase(
        name=BuildPhaseName.TEST, argv=("/usr/bin/pytest",), cwd="subproject"
    )
    policy = CommandPolicy(profile=profile(phase))
    assert policy.validate_phase("test", lease).cwd == (lease / "subproject").resolve()
    (lease / "subproject").rmdir()
    with pytest.raises(ValueError, match="cwd não existe"):
        policy.validate_phase("test", lease)
    (lease / "subproject").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="cwd não existe"):
        policy.validate_phase("test", lease)


@pytest.mark.parametrize("argument", ["tests", "--config=tests/config.toml"])
def test_repository_symlink_cannot_redirect_approved_argument(
    tmp_path: Path, argument: str
) -> None:
    lease = tmp_path / "lease"
    lease.mkdir()
    (lease / "tests").symlink_to(tmp_path, target_is_directory=True)
    phase = BuildPhase(name=BuildPhaseName.TEST, argv=("/usr/bin/pytest", argument))
    with pytest.raises(ValueError, match="fora da lease"):
        CommandPolicy(profile=profile(phase)).validate_phase("test", lease)


def test_retention_requires_timezone_for_safe_expiry_comparison() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        WorkspaceRetention(retain_until=datetime(2026, 1, 1))
    retention = WorkspaceRetention(
        retain_until=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert (
        WorkspaceRetention.model_validate_json(retention.model_dump_json()) == retention
    )
