import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.infrastructure.settings import Settings
from app.models.build import BuildPhase, BuildPhaseName, BuildProfile


def _python_profile() -> dict[str, object]:
    return {
        "ecosystem": "python",
        "image": "registry.example.com/test-python@sha256:" + "a" * 64,
        "auto_detect": True,
        "phases": [
            {
                "name": "test",
                "argv": [
                    "/usr/local/bin/python",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                ],
            }
        ],
    }


def test_factory_defaults_are_safe_and_disabled() -> None:
    settings = Settings(_env_file=None)

    assert settings.factory_mode_enabled is False
    assert settings.factory_command_backend == "docker"
    assert settings.factory_sandbox_network_enabled is False
    assert settings.factory_approved_scm_hosts == ["github.com"]
    assert settings.factory_build_profiles == {}
    assert settings.factory_repository_profiles == {}


@pytest.mark.parametrize(
    "hosts",
    [["*"], ["localhost"], ["127.0.0.1"], ["https://github.com"]],
)
def test_factory_rejects_unsafe_scm_hosts(hosts: list[str]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        Settings(
            _env_file=None,
            factory_mode_enabled=True,
            factory_approved_scm_hosts_json=json.dumps(hosts),
        )


def test_factory_requires_dedicated_workspace_root() -> None:
    with pytest.raises(ValidationError, match="diretório dedicado"):
        Settings(
            _env_file=None,
            factory_mode_enabled=True,
            factory_workspace_root=".",
        )


def test_factory_always_requires_docker() -> None:
    with pytest.raises(ValidationError, match="backend docker"):
        Settings(
            _env_file=None,
            factory_mode_enabled=True,
            factory_command_backend="local",
        )


def test_factory_parses_typed_profiles_and_repository_mapping() -> None:
    settings = Settings(
        _env_file=None,
        factory_mode_enabled=False,
        factory_build_profiles_json=json.dumps({"python-tests": _python_profile()}),
        factory_repository_profiles_json=json.dumps(
            {"example/service": "python-tests"}
        ),
    )

    profile = settings.factory_build_profiles["python-tests"]
    assert isinstance(profile, BuildProfile)
    assert profile.name == "python-tests"
    assert profile.ecosystem == "python"
    assert profile.auto_detect is True
    assert isinstance(profile.phases[0], BuildPhase)
    assert profile.phases[0].name == BuildPhaseName.TEST
    assert profile.phases[0].argv == (
        "/usr/local/bin/python",
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
    )
    assert profile.phases[0].network == "none"
    assert settings.factory_repository_profiles == {"example/service": "python-tests"}


def test_factory_profile_name_must_match_configuration_key() -> None:
    profile = {**_python_profile(), "name": "another-profile"}
    settings = Settings(
        _env_file=None,
        factory_mode_enabled=False,
        factory_build_profiles_json=json.dumps({"python-tests": profile}),
    )

    with pytest.raises(ValueError, match="diverge da chave administrada"):
        _ = settings.factory_build_profiles


def test_factory_profile_image_requires_digest() -> None:
    profile = {**_python_profile(), "image": "python:3.12-slim"}
    settings = Settings(
        _env_file=None,
        factory_mode_enabled=False,
        factory_build_profiles_json=json.dumps({"python-tests": profile}),
    )

    with pytest.raises(ValidationError, match="image"):
        _ = settings.factory_build_profiles


@pytest.mark.parametrize("raw", ["[]", "null", '"python-tests"', "42"])
@pytest.mark.parametrize(
    "setting_name, property_name",
    [
        ("factory_build_profiles_json", "factory_build_profiles"),
        ("factory_repository_profiles_json", "factory_repository_profiles"),
    ],
)
def test_factory_profile_configuration_requires_json_objects(
    raw: str, setting_name: str, property_name: str
) -> None:
    settings = Settings(
        _env_file=None,
        factory_mode_enabled=False,
        **{setting_name: raw},
    )

    with pytest.raises(ValueError, match="deve ser um objeto"):
        getattr(settings, property_name)


@pytest.mark.parametrize("profile", [[], None, "python-tests", 42])
def test_factory_build_profile_definition_requires_an_object(
    profile: object,
) -> None:
    settings = Settings(
        _env_file=None,
        factory_mode_enabled=False,
        factory_build_profiles_json=json.dumps({"python-tests": profile}),
    )

    with pytest.raises(ValueError, match="Cada perfil de build deve ser um objeto"):
        _ = settings.factory_build_profiles


def test_factory_enabled_accepts_valid_profiles_with_safe_configuration(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="prod",
        api_keys_json=json.dumps(
            {"test-production-key": {"client_id": "test", "projects": ["*"]}}
        ),
        factory_mode_enabled=True,
        factory_workspace_root=str(tmp_path / "factory-workspaces"),
        factory_approved_scm_hosts_json=json.dumps(["github.com"]),
        factory_command_backend="docker",
        factory_sandbox_network_enabled=False,
        factory_build_profiles_json=json.dumps({"python-tests": _python_profile()}),
        factory_repository_profiles_json=json.dumps(
            {"example/service": "python-tests"}
        ),
    )

    assert settings.factory_mode_enabled is True
    assert settings.factory_build_profiles["python-tests"].name == "python-tests"
    assert settings.factory_repository_profiles["example/service"] == "python-tests"


def test_factory_enabled_rejects_mapping_to_missing_profile(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="perfil mapeado desconhecido"):
        Settings(
            _env_file=None,
            factory_mode_enabled=True,
            factory_workspace_root=str(tmp_path / "factory-workspaces"),
            factory_approved_scm_hosts_json=json.dumps(["github.com"]),
            factory_command_backend="docker",
            factory_sandbox_network_enabled=False,
            factory_build_profiles_json=json.dumps({"python-tests": _python_profile()}),
            factory_repository_profiles_json=json.dumps(
                {"example/service": "missing-profile"}
            ),
        )


@pytest.mark.parametrize(
    "profiles_json, mappings_json, error",
    [
        ("[]", "{}", "FACTORY_BUILD_PROFILES_JSON deve ser um objeto"),
        ("{}", "[]", "FACTORY_REPOSITORY_PROFILES_JSON deve ser um objeto"),
        (
            json.dumps(
                {"python-tests": {**_python_profile(), "name": "another-profile"}}
            ),
            "{}",
            "diverge da chave administrada",
        ),
        (
            json.dumps(
                {"python-tests": {**_python_profile(), "image": "python:3.12-slim"}}
            ),
            "{}",
            "image",
        ),
    ],
)
def test_factory_enabled_validates_profiles_during_settings_construction(
    tmp_path: Path, profiles_json: str, mappings_json: str, error: str
) -> None:
    with pytest.raises(ValidationError, match=error):
        Settings(
            _env_file=None,
            factory_mode_enabled=True,
            factory_workspace_root=str(tmp_path / "factory-workspaces"),
            factory_approved_scm_hosts_json=json.dumps(["github.com"]),
            factory_command_backend="docker",
            factory_sandbox_network_enabled=False,
            factory_build_profiles_json=profiles_json,
            factory_repository_profiles_json=mappings_json,
        )
