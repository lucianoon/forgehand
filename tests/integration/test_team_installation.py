"""Opt-in installation drill: real Compose, PostgreSQL, Git and Docker builds.

Only the workflow graph is replaced by a mounted deterministic fixture. Runtime
code has no test switch, and the fixture denies outbound model/SCM HTTP requests.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4

import httpx
import pytest

from app.cli import main as cli_main

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    os.getenv("RUN_TEAM_INSTALLATION_TESTS") != "1",
    reason="requires an isolated Docker host and a built team runtime image",
)
PYTHON_IMAGE = "python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"


def _wait(check, *, timeout=90):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (httpx.HTTPError, OSError, ValueError) as exc:
            last_error = type(exc).__name__
        time.sleep(0.2)
    raise AssertionError(f"installation condition did not become true ({last_error})")


def _env_file(path, values):
    # Test-only generated values; real installation credentials are never read.
    assert all("'" not in value and "\n" not in value for value in values.values())
    path.write_text("\n".join(f"{key}='{value}'" for key, value in values.items()) + "\n")
    path.chmod(0o600)


def test_team_installation_crash_backup_and_restore(tmp_path, capsys):
    docker = shutil.which("docker")
    assert docker is not None, "Docker CLI is required"
    image = os.environ.get("FORGEHAND_TEAM_TEST_IMAGE")
    assert image, "build the runtime and set FORGEHAND_TEAM_TEST_IMAGE"
    project = "forgehand-teamtest-" + uuid4().hex[:12]
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    probe = data / "probe"
    repository = probe / "repository"
    repository.mkdir(parents=True)
    (repository / "orders.py").write_text("def total(values):\n    return sum(values)\n")
    (repository / ".env.example").write_text("PROJECT_EXAMPLE=fixture-only\n")
    (repository / ".env").write_text("PROJECT_FIXTURE=not-a-real-secret\n")
    (repository / "tests").mkdir()
    (repository / "tests" / "test_installation.py").write_text(
        "import os,unittest\nfrom orders import total\n"
        "class InstallationTest(unittest.TestCase):\n"
        " def test_mount_and_environment(self):\n"
        "  self.assertEqual(total([2,3]),5)\n"
        "  self.assertNotIn('OPENAI_API_KEY',os.environ)\n"
        "  self.assertNotIn('GITHUB_TOKEN',os.environ)\n"
    )
    git_env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (
        ["init", "-b", "main"], ["add", "."],
        ["-c", "user.name=Installation fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Fixture"],
    ):
        subprocess.run(["git", *args], cwd=repository, env=git_env, capture_output=True, check=True)
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    admin_key = "installation-admin-fixture"
    env_path = tmp_path / "team.env"
    socket_path = os.environ.get("FACTORY_DOCKER_SOCKET", "/var/run/docker.sock")
    assert Path(socket_path).is_socket()
    profile = {
        "installation-fixture": {
            "name": "installation-fixture", "ecosystem": "python", "image": PYTHON_IMAGE,
            "phases": [
                {"name": "build", "argv": ["/usr/local/bin/python", "-m", "compileall", "-q", "orders.py"]},
                {"name": "test", "argv": ["/usr/local/bin/python", "-m", "unittest", "discover", "-s", "tests"]},
            ],
        },
    }
    values = {
        "TEAM_ENV_FILE": str(env_path), "FORGEHAND_IMAGE": image,
        "FORGEHAND_REVISION": "d" * 40, "FORGEHAND_DATA_ROOT": str(data),
        "FORGEHAND_UID": str(os.getuid()), "FORGEHAND_GID": str(os.getgid()),
        "DOCKER_SOCKET_PATH": socket_path, "DOCKER_SOCKET_GID": os.environ.get("TEAM_TEST_DOCKER_SOCKET_GID", str(Path(socket_path).stat().st_gid)),
        "APP_PORT": "0", "APP_BIND_ADDRESS": "127.0.0.1", "INSTALLATION_EXPECTED_WORKERS": "2",
        "POSTGRES_IMAGE": os.environ.get("FORGEHAND_TEAM_POSTGRES_IMAGE", "postgres:16-alpine"),
        "POSTGRES_PASSWORD": "installation-db-fixture",
        "DATABASE_URL": "postgresql://forgehand:installation-db-fixture@postgres:5432/forgehand",
        "API_KEYS_JSON": json.dumps({
            admin_key: {"client_id": "team-admin", "projects": ["team-test"], "role": "admin"},
            "installation-other-fixture": {"client_id": "other", "projects": ["team-test"], "role": "approver"},
        }),
        "LLM_PROVIDER_BACKEND": "openai", "OPENAI_API_KEY": "not-a-real-openai-key",
        "OPENROUTER_API_KEY": "", "ANTHROPIC_API_KEY": "",
        "GITHUB_TOKEN": "not-a-real-github-token", "GITHUB_APP_ID": "", "GITHUB_APP_INSTALLATION_ID": "", "GITHUB_APP_PRIVATE_KEY": "",
        "FACTORY_BUILD_PROFILES_JSON": json.dumps(profile), "FACTORY_REPOSITORY_PROFILES_JSON": "{}",
        "WORKFLOW_QUEUE_LEASE_SECONDS": "2", "WORKFLOW_QUEUE_POLL_INTERVAL_SECONDS": "0.1",
        "FACTORY_SUCCESS_RETENTION_SECONDS": "3600",
    }
    _env_file(env_path, values)
    override = tmp_path / "probe-compose.json"
    mounts = [{"type": "bind", "source": str(ROOT / "tests/fixtures/team_installation_runtime.py"), "target": "/probe/runtime.py", "read_only": True}]
    override.write_text(json.dumps({"services": {
        "api": {"volumes": mounts, "environment": {"PYTHONPATH": "/srv/forgehand"}, "command": ["python", "-m", "app.operations.team_backup", "run", "--data-root", str(data), "--", "python", "/probe/runtime.py", "api"]},
        "worker": {"volumes": mounts, "environment": {"PYTHONPATH": "/srv/forgehand"}, "command": ["/bin/sh", "-c", 'exec env AUDIT_LOG_PATH="$$FORGEHAND_DATA_ROOT/audit/worker-$$HOSTNAME.jsonl" python -m app.operations.team_backup run --data-root "$$FORGEHAND_DATA_ROOT" -- python /probe/runtime.py worker']},
    }}))
    # Prevent shell variables from overriding this isolated fixture's topology.
    environment = {key: value for key, value in os.environ.items() if key not in values}

    def command(args, *, check=True, timeout=120):
        result = subprocess.run([docker, *args], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=timeout)
        if check:
            assert result.returncode == 0, result.stderr[-5000:]
        return result

    def compose(*args, check=True, timeout=120):
        return command(["compose", "-p", project, "--env-file", str(env_path), "-f", str(ROOT / "docker-compose.team.yml"), "-f", str(override), *args], check=check, timeout=timeout)

    def api_url():
        return "http://" + compose("port", "api", "8000").stdout.strip()

    client = None
    try:
        command(["image", "inspect", image])
        command(["image", "inspect", PYTHON_IMAGE])
        compose("up", "-d", "--no-build", "--scale", "worker=2", timeout=180)
        url = api_url()
        client = httpx.Client(base_url=url, headers={"X-API-Key": admin_key}, timeout=5)
        _wait(lambda: client.get("/readyz").status_code == 200)
        assert cli_main(["--url", url, "--api-key", admin_key, "doctor", "--json"]) == 0
        diagnostic = json.loads(capsys.readouterr().out)
        assert diagnostic["workers"]["compatible"] >= 2 and diagnostic["ready"]
        assert diagnostic["configuration"]["workspace_root"] == str(data / "factory")
        order = {"project_id": "team-test", "work_order": {
            "repository": "fixture/installation", "requested_outcome": "Verify deterministic installation recovery",
            "acceptance_criteria": ["Fixture builds without credentials"], "build_profile": "installation-fixture",
            "idempotency_key": "installation-drill-1", "limits": {"max_tokens": 100, "max_cost_usd": 0.5, "max_iterations": 2},
        }}
        response = client.post("/workflows", json=order)
        assert response.status_code == 202, response.text
        workflow = response.json()["workflow_id"]
        _wait(lambda: (probe / "blocked.json").exists())
        before = _wait(lambda: (state if (state := client.get(f"/workflows/{workflow}").json()).get("usage", {}).get("tokens") == 7 and state.get("workspace") else None))
        blocked = json.loads((probe / "blocked.json").read_text())
        workers = compose("ps", "-q", "worker").stdout.splitlines()
        victim = next(identifier for identifier in workers if identifier.startswith(blocked["hostname"]))
        command(["update", "--restart=no", victim])
        command(["kill", "--signal=KILL", victim])
        (probe / "release").touch()
        paused = _wait(lambda: (state if (state := client.get(f"/workflows/{workflow}").json()).get("status") == "awaiting_decision" else None))
        assert paused["pending_decision"]["reason"] == "installation_approval"
        assert paused["usage"] == before["usage"] and paused["budget"] == before["budget"]
        assert paused["workspace"]["id"] == before["workspace"]["id"]
        event_list = [json.loads(line) for line in (probe / "events.jsonl").read_text().splitlines()]
        assert sum(event["event"] == "prepare" for event in event_list) == 1
        assert sum(event["event"] == "build_success" for event in event_list) == 1
        assert any(event["event"] == "build_success" and event["hostname"] != blocked["hostname"] for event in event_list)
        assert client.post("/workflows", json=order).json()["workflow_id"] == workflow
        assert client.get(f"/workflows/{workflow}", headers={"X-API-Key": "installation-other-fixture"}).status_code == 403
        # Maintenance must refuse a live installation before producing a backup.
        refused = compose("run", "--rm", "--no-deps", "-T", "-v", f"{backups}:{backups}", "worker", "python", "-m", "app.operations.team_backup", "backup", "--data-root", str(data), "--output", str(backups / "active"), check=False)
        assert refused.returncode != 0 and not (backups / "active").exists()
        compose("stop", "-t", "10", "api", "worker")
        saved = backups / "snapshot"
        backup_environment = tmp_path / "backup-runtime.env"
        backup_environment.write_text(
            "DATABASE_URL=" + values["DATABASE_URL"] + "\n"
            + "FORGEHAND_REVISION=" + values["FORGEHAND_REVISION"] + "\n"
        )
        backup_environment.chmod(0o600)
        # Mount the dedicated parent for both maintenance operations. Docker Desktop
        # reports bind mount roots as uid 0, while child entries retain real ownership.
        command(["run", "--rm", "--network", project + "_default", "--user", f"{os.getuid()}:{os.getgid()}", "--env-file", str(backup_environment), "--mount", f"type=bind,source={tmp_path},target={tmp_path}", image, "python", "-m", "app.operations.team_backup", "backup", "--data-root", str(data), "--output", str(saved)])
        assert (saved / "manifest.json").is_file()
        # Restore into a new database. Keep all source bytes in a separate directory.
        compose("exec", "-T", "postgres", "createdb", "-U", "forgehand", "forgehand_restored")
        preserved = tmp_path / "source-data-preserved"
        data.rename(preserved)
        values["DATABASE_URL"] = "postgresql://forgehand:installation-db-fixture@postgres:5432/forgehand_restored"
        _env_file(env_path, values)
        restore_environment = tmp_path / "restore-runtime.env"
        restore_environment.write_text(
            "RESTORE_DATABASE_URL=" + values["DATABASE_URL"] + "\n"
            + "FORGEHAND_REVISION=" + values["FORGEHAND_REVISION"] + "\n"
        )
        restore_environment.chmod(0o600)
        command(["run", "--rm", "--network", project + "_default", "--user", f"{os.getuid()}:{os.getgid()}", "--env-file", str(restore_environment), "--mount", f"type=bind,source={tmp_path},target={tmp_path}", image, "python", "-m", "app.operations.team_backup", "restore", "--backup", str(saved), "--data-root", str(data), "--original-path"])
        journal_path = Path("factory/control/lifecycle.sqlite3")
        assert (data / journal_path).read_bytes() == (preserved / journal_path).read_bytes()
        assert (data / "probe/events.jsonl").read_bytes() == (preserved / "probe/events.jsonl").read_bytes()
        restored_checkout = Path(paused["workspace"]["local_path"])
        status = subprocess.run(["git", "status", "--porcelain"], cwd=restored_checkout, env=git_env, capture_output=True, text=True, check=True)
        assert not status.stdout, "Restore must not remove versioned project configuration"
        assert (restored_checkout / ".env.example").read_text() == "PROJECT_EXAMPLE=fixture-only\n"
        assert (restored_checkout / ".env").read_text() == "PROJECT_FIXTURE=not-a-real-secret\n"
        compose("up", "-d", "--no-build", "--force-recreate", "--scale", "worker=2", "api", "worker", timeout=180)
        client.close()
        client = httpx.Client(base_url=api_url(), headers={"X-API-Key": admin_key}, timeout=5)
        _wait(lambda: client.get("/readyz").status_code == 200)
        restored = client.get(f"/workflows/{workflow}").json()
        assert restored["pending_decision"] == paused["pending_decision"]
        assert restored["usage"] == paused["usage"] and restored["budget"] == paused["budget"]
        assert restored["workspace"]["id"] == paused["workspace"]["id"]
        # Reconciliation may append retention events after the initial pause;
        # the original history must remain intact, with no new provisioning.
        assert restored["workspace_history"][:len(paused["workspace_history"])] == paused["workspace_history"]
        assert all(event["state"] == "retained" for event in restored["workspace_history"][len(paused["workspace_history"]):])
        assert client.post("/workflows", json=order).json()["workflow_id"] == workflow
        assert client.get(f"/workflows/{workflow}", headers={"X-API-Key": "installation-other-fixture"}).status_code == 403
        assert client.post(f"/workflows/{workflow}/decision", json={"decision": "retry"}).status_code == 202
        completed = _wait(lambda: (state if (state := client.get(f"/workflows/{workflow}").json()).get("status") == "completed" else None))
        assert completed["usage"] == paused["usage"]
        final_events = [json.loads(line) for line in (data / "probe/events.jsonl").read_text().splitlines()]
        assert sum(event["event"] == "prepare" for event in final_events) == 1
        assert sum(event["event"] == "build_success" for event in final_events) == 1
        print(json.dumps({"workflow_id": workflow, "crash_recovered": True, "backup_restored": True, "pending_approval_preserved": True, "idempotency_preserved": True, "owner_preserved": True, "budget_preserved": True, "docker_build_passed": True, "outbound_model_calls": 0}))
    finally:
        if client is not None:
            client.close()
        compose("down", "--volumes", "--remove-orphans", check=False, timeout=120)
