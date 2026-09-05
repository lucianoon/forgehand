"""Validate the operator-facing Compose contract without starting services."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKER = shutil.which("docker")
pytestmark = pytest.mark.skipif(DOCKER is None, reason="Docker CLI with Compose is required for configuration validation")


def rendered_compose(tmp_path, *, data_root=None, missing_env_file=False, uid="1000", gid="1000"):
    directory = data_root if data_root is not None else str(tmp_path / "persistent")
    env_file = tmp_path / "server.env"
    values = {
        "TEAM_ENV_FILE": str(tmp_path / "missing.env") if missing_env_file else str(env_file),
        "FORGEHAND_IMAGE": "forgehand:team-test", "FORGEHAND_REVISION": "a" * 40,
        "FORGEHAND_DATA_ROOT": directory, "DOCKER_SOCKET_GID": "998",
        "FORGEHAND_UID": uid, "FORGEHAND_GID": gid,
        "DOCKER_SOCKET_PATH": "/var/run/docker.sock", "POSTGRES_PASSWORD": "test-db-password",
        "DATABASE_URL": "postgresql://forgehand:test-db-password@postgres:5432/forgehand",
        "API_KEYS_JSON": '{"test-admin":{"client_id":"team","projects":["test"],"role":"admin"}}',
        "LLM_PROVIDER_BACKEND": "openai", "OPENAI_API_KEY": "test-openai-$literal",
        "GITHUB_TOKEN": "test-github-token", "GITHUB_APP_ID": "test-app",
        "GITHUB_APP_INSTALLATION_ID": "test-installation",
        "GITHUB_APP_PRIVATE_KEY": "not-a-real-key\\nsecond-line",
        "FACTORY_BUILD_PROFILES_JSON": '{}', "INSTALLATION_EXPECTED_WORKERS": "2",
    }
    env_file.write_text("\n".join(f"{key}='{value}'" for key, value in values.items()) + "\n")
    environment = os.environ.copy()
    # Shell environment takes precedence over --env-file. Isolate all topology
    # fields and the credential examples from unrelated operator/CI settings.
    for key in (*values, "APP_BIND_ADDRESS", "APP_PORT", "POSTGRES_IMAGE"):
        environment.pop(key, None)
    result = subprocess.run(
        [DOCKER, "compose", "--env-file", str(env_file), "-f", str(ROOT / "docker-compose.team.yml"),
         "config", "--format", "json"],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30,
    )
    return result, values


def test_team_runtime_shares_factory_contract_and_credentials(tmp_path):
    result, values = rendered_compose(tmp_path)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    api, worker = services["api"], services["worker"]
    assert api["image"] == worker["image"] == values["FORGEHAND_IMAGE"]
    assert api["build"] == worker["build"]
    assert api["build"]["args"]["FORGEHAND_REVISION"] == values["FORGEHAND_REVISION"]
    for key in ("FORGEHAND_REVISION", "DATABASE_URL", "API_KEYS_JSON", "OPENAI_API_KEY", "GITHUB_TOKEN",
                "GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY", "FACTORY_BUILD_PROFILES_JSON"):
        assert api["environment"][key] == worker["environment"][key]
        # Compose config escapes literal dollars for a reusable rendered file.
        assert api["environment"][key].replace("$$", "$") == values[key]
    for runtime in (api, worker):
        assert runtime["environment"]["FACTORY_MODE_ENABLED"] == "true"
        assert runtime["environment"]["CHECKPOINTER_BACKEND"] == "postgres"
        assert runtime["environment"]["WORKFLOW_QUEUE_BACKEND"] == "postgres"
        assert runtime["environment"]["INSTALLATION_EXPECTED_WORKERS"] == "2"
        assert runtime["environment"]["WORKFLOW_WORKER_CONCURRENCY"] == "1"
    assert api["environment"]["RUN_EMBEDDED_WORKFLOW_WORKERS"] == "false"
    assert worker["environment"]["RUN_EMBEDDED_WORKFLOW_WORKERS"] == "true"
    assert "container_name" not in worker and "ports" not in worker


def test_team_paths_preserve_sibling_mount_identity_and_process_audits(tmp_path):
    result, values = rendered_compose(tmp_path)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    data_root = values["FORGEHAND_DATA_ROOT"]
    for name in ("api", "worker"):
        runtime = services[name]
        data_mount = next(mount for mount in runtime["volumes"] if mount["target"] == data_root)
        assert data_mount["type"] == "bind" and data_mount["source"] == data_root
        assert data_mount.get("bind", {}).get("create_host_path", False) is False
        assert runtime["environment"]["FACTORY_WORKSPACE_ROOT"] == data_root + "/factory"
        assert runtime["environment"]["EXECUTOR_WORKSPACE_ROOT"] == data_root + "/executor"
        assert "app.operations.team_backup" in " ".join(runtime["command"])
        assert "--data-root" in " ".join(runtime["command"])
    assert services["api"]["environment"]["AUDIT_LOG_PATH"] == data_root + "/audit/api.jsonl"
    assert "worker-" in services["worker"]["command"][-1]
    assert "HOSTNAME" in services["worker"]["command"][-1]
    assert services["worker"]["command"][-1].startswith("exec env ")


def test_team_exposure_and_socket_access_are_explicit(tmp_path):
    result, _ = rendered_compose(tmp_path)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert "ports" not in services["postgres"]
    assert services["api"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert services["api"]["ports"][0]["target"] == 8000
    assert "OPENAI_API_KEY" not in services["postgres"]["environment"]
    for name in ("api", "worker"):
        runtime = services[name]
        assert runtime["user"] == "1000:1000" and runtime["read_only"]
        assert runtime["group_add"] == ["998"]
        assert "ALL" in runtime["cap_drop"]
        assert not runtime.get("privileged", False)
        socket = next(m for m in runtime["volumes"] if m["target"] == "/var/run/docker.sock")
        assert socket["source"] == "/var/run/docker.sock"


def test_team_requires_a_real_server_env_file(tmp_path):
    result, _ = rendered_compose(tmp_path, missing_env_file=True)
    assert result.returncode != 0


@pytest.mark.skipif(not os.getenv("FORGEHAND_TEAM_TEST_IMAGE"), reason="set the locally built team image for runtime verification")
def test_built_team_image_contains_operational_tools_without_root():
    image = os.environ["FORGEHAND_TEAM_TEST_IMAGE"]
    script = """
import importlib, os, shutil, subprocess
assert os.getuid() == 1000
for name in ('jwt', 'psycopg', 'langgraph.checkpoint.postgres'):
    importlib.import_module(name)
for tool in ('git', 'docker', 'pg_dump', 'pg_restore', 'forgehand'):
    assert shutil.which(tool), tool
for tool in ('pg_dump', 'pg_restore'):
    assert ' 16.' in subprocess.check_output([tool, '--version'], text=True)
assert os.path.isfile('/etc/ssl/certs/ca-certificates.crt')
assert os.environ.get('FORGEHAND_REVISION')
print('runtime_contract_ok')
"""
    result = subprocess.run(
        [DOCKER, "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
         "--security-opt", "no-new-privileges", "--entrypoint", "python", image, "-c", script],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "runtime_contract_ok"


def test_team_can_match_nonroot_host_uid_and_gid(tmp_path):
    result, _ = rendered_compose(tmp_path, uid="501", gid="20")
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert services["api"]["user"] == services["worker"]["user"] == "501:20"
