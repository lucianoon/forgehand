"""Opt-in bounded concurrency and PostgreSQL outage drill on real Compose.

Twelve synthetic workflows are a correctness experiment, not a load benchmark.
Only the graph is a mounted fixture; no production test mode or real API key is
used. All destructive commands target one fresh UUID-named Compose project.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
from threading import Barrier
from uuid import uuid4

import httpx
import pytest

from tests.integration.test_team_installation import _env_file, _wait


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    os.getenv("RUN_TEAM_INSTALLATION_TESTS") != "1",
    reason="requires an isolated Docker host and a built team runtime image",
)


def test_concurrent_intake_and_postgres_outage_recover_without_lost_work(tmp_path):
    docker = shutil.which("docker")
    assert docker is not None, "Docker CLI is required"
    image = os.environ.get("FORGEHAND_TEAM_TEST_IMAGE")
    assert image, "build the runtime and set FORGEHAND_TEAM_TEST_IMAGE"
    project = "forgehand-resilience-" + uuid4().hex[:12]
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    probe = data / "probe"
    probe.mkdir()
    env_path = tmp_path / "team.env"
    socket_path = os.environ.get("FACTORY_DOCKER_SOCKET", "/var/run/docker.sock")
    assert Path(socket_path).is_socket()
    keys = {"alice": "resilience-alice-fixture", "bob": "resilience-bob-fixture"}
    values = {
        "TEAM_ENV_FILE": str(env_path), "FORGEHAND_IMAGE": image,
        "FORGEHAND_REVISION": "e" * 40, "FORGEHAND_DATA_ROOT": str(data),
        "FORGEHAND_UID": str(os.getuid()), "FORGEHAND_GID": str(os.getgid()),
        "DOCKER_SOCKET_PATH": socket_path,
        "DOCKER_SOCKET_GID": os.environ.get("TEAM_TEST_DOCKER_SOCKET_GID", str(Path(socket_path).stat().st_gid)),
        "APP_PORT": "0", "APP_BIND_ADDRESS": "127.0.0.1", "INSTALLATION_EXPECTED_WORKERS": "2",
        "POSTGRES_IMAGE": os.environ.get("FORGEHAND_TEAM_POSTGRES_IMAGE", "postgres:16-alpine"),
        "POSTGRES_PASSWORD": "resilience-db-fixture",
        "DATABASE_URL": "postgresql://forgehand:resilience-db-fixture@postgres:5432/forgehand",
        "API_KEYS_JSON": json.dumps({
            key: {"client_id": owner, "projects": ["shared-project"], "role": "admin"}
            for owner, key in keys.items()
        }),
        "LLM_PROVIDER_BACKEND": "openai", "OPENAI_API_KEY": "not-a-real-openai-key",
        "OPENROUTER_API_KEY": "", "ANTHROPIC_API_KEY": "",
        "GITHUB_TOKEN": "not-a-real-github-token", "GITHUB_APP_ID": "",
        "GITHUB_APP_INSTALLATION_ID": "", "GITHUB_APP_PRIVATE_KEY": "",
        "FACTORY_BUILD_PROFILES_JSON": "{}", "FACTORY_REPOSITORY_PROFILES_JSON": "{}",
        "WORKFLOW_QUEUE_LEASE_SECONDS": "2", "WORKFLOW_QUEUE_POLL_INTERVAL_SECONDS": "0.1",
    }
    _env_file(env_path, values)
    mounts = [{"type": "bind", "source": str(ROOT / "tests/fixtures/team_resilience_runtime.py"),
               "target": "/probe/runtime.py", "read_only": True}]
    override = tmp_path / "probe-compose.json"
    override.write_text(json.dumps({"services": {
        "api": {"volumes": mounts, "environment": {"PYTHONPATH": "/srv/forgehand"},
                "command": ["python", "-m", "app.operations.team_backup", "run", "--data-root", str(data),
                            "--", "python", "/probe/runtime.py", "api"]},
        "worker": {"volumes": mounts, "environment": {"PYTHONPATH": "/srv/forgehand"},
                   "command": ["/bin/sh", "-c", 'exec env AUDIT_LOG_PATH="$$FORGEHAND_DATA_ROOT/audit/worker-$$HOSTNAME.jsonl" python -m app.operations.team_backup run --data-root "$$FORGEHAND_DATA_ROOT" -- python /probe/runtime.py worker']},
    }}))
    # Unrelated shell settings cannot override this disposable installation.
    environment = {key: value for key, value in os.environ.items() if key not in values}

    def compose(*args, check=True, timeout=120):
        result = subprocess.run(
            [docker, "compose", "-p", project, "--env-file", str(env_path),
             "-f", str(ROOT / "docker-compose.team.yml"), "-f", str(override), *args],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=timeout,
        )
        if check:
            assert result.returncode == 0, result.stderr[-5000:]
        return result

    def runtime_processes():
        identifiers = compose("ps", "-q", "api", "worker").stdout.splitlines()
        assert len(identifiers) == 3
        result = subprocess.run(
            [docker, "inspect", "--format", "{{.Id}} {{.State.StartedAt}} {{.RestartCount}}", *identifiers],
            env=environment, capture_output=True, text=True, check=True, timeout=10,
        )
        return sorted(result.stdout.splitlines())

    def events():
        path = probe / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    client = None
    try:
        compose("up", "-d", "--no-build", "--scale", "worker=2", timeout=180)
        url = "http://" + compose("port", "api", "8000").stdout.strip()
        client = httpx.Client(base_url=url, timeout=5)
        _wait(lambda: client.get("/readyz").status_code == 200)
        original_processes = runtime_processes()
        orders = [{
            "project_id": "shared-project", "work_order": {
                "repository": "fixture/resilience",
                "requested_outcome": f"Complete resilience fixture case {index:02d}",
                "acceptance_criteria": ["Complete exactly one fixture effect"],
                # The first two owners intentionally reuse the same key.
                "idempotency_key": "shared-owner-key" if index < 2 else f"resilience-{index}",
                "limits": {"max_tokens": 100 + index, "max_cost_usd": (100 + index) / 1000,
                           "max_iterations": 2, "max_wall_clock_seconds": 600},
            },
        } for index in range(12)]
        owners = ["alice" if index % 2 == 0 else "bob" for index in range(12)]
        # Twenty simultaneous admissions: twelve unique requests, four extra
        # copies of each owner's shared key. All must converge to twelve IDs.
        submissions = [*range(12), *([0, 1] * 4)]
        barrier = Barrier(len(submissions))

        def submit(index):
            barrier.wait(timeout=20)
            response = client.post("/workflows", json=orders[index], headers={"X-API-Key": keys[owners[index]]})
            assert response.status_code == 202, response.text
            return index, response.json()["workflow_id"]

        with ThreadPoolExecutor(max_workers=len(submissions)) as executor:
            admitted = list(executor.map(submit, submissions))
        workflow_ids = {index: workflow for index, workflow in admitted}
        assert len(set(workflow_ids.values())) == 12
        assert all(workflow_ids[index] == workflow for index, workflow in admitted)

        def queue():
            response = client.get("/metrics")
            response.raise_for_status()
            return response.json()["queue"]

        _wait(lambda: (stats if (stats := queue())["processing"] == 2 and stats["queued"] == 10 else None))
        holding = _wait(lambda: (items if len(items := [item for item in events() if item["event"] == "holding"]) == 2 else None))
        assert len({item["hostname"] for item in holding}) == 2
        before = {}
        for item in holding:
            index = next(index for index, identifier in workflow_ids.items() if identifier == item["workflow_id"])
            response = client.get(f"/workflows/{workflow_ids[index]}", headers={"X-API-Key": keys[owners[index]]})
            assert response.status_code == 200
            before[index] = response.json()
            assert before[index]["usage"] == {"tokens": 7 + index, "cost_usd": 0.0}
            assert before[index]["budget"] == orders[index]["work_order"]["limits"]

        # Interrupt the real database with accepted jobs still queued/in flight.
        # The API and workers are never restarted by the test during recovery.
        compose("stop", "-t", "2", "postgres")
        unavailable = _wait(lambda: (response if (response := client.get("/readyz")).status_code == 503 else None))
        assert unavailable.json()["queue_ready"] is False
        assert client.get("/health").status_code == 200
        compose("start", "postgres")
        _wait(lambda: client.get("/readyz").status_code == 200, timeout=120)
        (probe / "release").touch()

        completed = {}

        def all_completed():
            for index, workflow in workflow_ids.items():
                response = client.get(f"/workflows/{workflow}", headers={"X-API-Key": keys[owners[index]]})
                response.raise_for_status()
                state = response.json()
                assert state["status"] not in {"failed", "cancelled"}, state
                if state["status"] == "completed":
                    completed[index] = state
            return len(completed) == 12

        _wait(all_completed, timeout=120)
        stats = _wait(lambda: (stats if (stats := queue())["done"] == 12 and stats["queued"] == stats["processing"] == 0 else None))
        assert stats["failed"] == stats["expired_processing"] == 0
        for index, state in completed.items():
            assert state["usage"] == {"tokens": 7 + index, "cost_usd": 0.0}
            assert state["budget"] == orders[index]["work_order"]["limits"]
            assert state["final_output"] == f"Completed fixture for {owners[index]}/shared-project."
            other = "bob" if owners[index] == "alice" else "alice"
            assert client.get(f"/workflows/{workflow_ids[index]}", headers={"X-API-Key": keys[other]}).status_code == 403
        for owner in keys:
            response = client.get("/workflows?limit=100", headers={"X-API-Key": keys[owner]})
            response.raise_for_status()
            actual = {item["workflow_id"] for item in response.json()}
            assert actual == {workflow for index, workflow in workflow_ids.items() if owners[index] == owner}
        # Replaying after recovery must not create jobs, checkpoints or effects.
        for index in (0, 1):
            response = client.post("/workflows", json=orders[index], headers={"X-API-Key": keys[owners[index]]})
            assert response.status_code == 202
            assert response.json()["workflow_id"] == workflow_ids[index]
        final_events = events()
        for event in ("prepare", "effect"):
            assert Counter(item["workflow_id"] for item in final_events if item["event"] == event) == Counter(workflow_ids.values())
        after_replay = queue()
        assert after_replay["done"] == 12
        assert after_replay["queued"] == after_replay["processing"] == after_replay["failed"] == 0
        assert runtime_processes() == original_processes
        print(json.dumps({
            "compose_project": project, "unique_workflows": 12, "concurrent_intakes": len(submissions),
            "database_outage_observed": True, "recovered_without_runtime_restart": True,
            "queue_drained": True, "owners_and_budgets_preserved": True,
            "prepare_effects": 12, "completion_effects": 12, "outbound_model_calls": 0,
            "scope": "bounded correctness drill, not a throughput benchmark",
        }))
    finally:
        if client is not None:
            client.close()
        # Preserve diagnostic logs on failure in pytest's temporary directory.
        logs = compose("logs", "--no-color", check=False)
        (tmp_path / "compose.log").write_text(logs.stdout + logs.stderr)
        compose("down", "--volumes", "--remove-orphans", check=False, timeout=120)
