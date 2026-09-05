import json
from copy import deepcopy

import httpx
import pytest

from app.cli import main


def report(ready=True):
    return {
        "schema_version": 1, "ready": ready, "factory_mode": True,
        "revision": "test-revision", "fingerprint": "a" * 64,
        "expected_workers": 2, "jobs": {"incompatible": 0, "legacy_unbound": 0, "unconfigured": 0},
        "checks": [{"name": "workers", "status": "pass" if ready else "fail", "code": "compatible" if ready else "workers_unavailable", "detail": "Verifique os workers."}],
        "workers": {"active": 2 if ready else 0, "compatible": 2 if ready else 0, "incompatible": 0, "legacy": 0},
        "configuration": {"workspace_root": "/srv/team/factory", "workspace_identity": "test-workspace", "source_digest": "c" * 64, "dependency_network_enabled": False, "approved_scm_hosts": ["github.com"], "build_profiles_digest": "b" * 64, "queue_backend": "postgres", "checkpointer_backend": "postgres", "command_backend": "docker", "docker_socket": "/var/run/docker.sock"},
    }


@pytest.mark.parametrize("options", [["doctor", "--json"], ["--json", "doctor"], ["doctor"]])
@pytest.mark.parametrize("ready", [True, False])
def test_doctor_reads_installation_without_starting_work(options, ready, capsys):
    calls = []
    payload = report(ready)
    payload["unknown_sensitive_field"] = "never-echo-this"

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path == "/operations/installation"
        assert not request.content
        assert request.headers["X-API-Key"] == "admin-fixture"
        return httpx.Response(200 if ready else 503, json=payload)

    assert main(["--api-key", "admin-fixture", *options], transport=httpx.MockTransport(handle)) == (0 if ready else 1)
    captured = capsys.readouterr()
    assert len(calls) == 1 and not captured.err
    assert "never-echo-this" not in captured.out
    if "--json" in options:
        assert json.loads(captured.out) == report(ready)
    else:
        assert "Workers compatíveis" in captured.out


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_doctor_does_not_echo_error_bodies(status, capsys):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text="secret-response"))
    assert main(["doctor"], transport=transport) == 2
    captured = capsys.readouterr()
    assert "secret-response" not in captured.out + captured.err


@pytest.mark.parametrize("mutation", [
    lambda data: data.update(schema_version=2),
    lambda data: data.update(ready="true"),
    lambda data: data.update(checks=[]),
    lambda data: data["workers"].update(compatible=-1),
    lambda data: data["checks"][0].update(status="fail"),
])
def test_doctor_rejects_invalid_or_contradictory_reports(mutation, capsys):
    payload = deepcopy(report())
    mutation(payload)
    payload["secret"] = "never-echo-this"
    assert main(["doctor", "--json"], transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) == 2
    captured = capsys.readouterr()
    assert not captured.out and "never-echo-this" not in captured.err


def test_doctor_transport_failure_has_no_submission_hint(capsys):
    def fail(request):
        raise httpx.ConnectError("sensitive URL", request=request)

    assert main(["doctor"], transport=httpx.MockTransport(fail)) == 2
    captured = capsys.readouterr()
    assert "sensitive" not in captured.err and "idempotency-key" not in captured.err
