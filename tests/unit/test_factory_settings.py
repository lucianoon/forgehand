import json

import pytest
from pydantic import ValidationError

from app.infrastructure.settings import Settings


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


def test_factory_in_production_requires_docker() -> None:
    with pytest.raises(ValidationError, match="backend docker"):
        Settings(
            _env_file=None,
            environment="prod",
            api_keys_json=json.dumps(
                {"prod-key": {"client_id": "prod", "projects": ["*"]}}
            ),
            factory_mode_enabled=True,
            factory_command_backend="local",
        )
