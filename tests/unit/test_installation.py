"""Deployment identity and operational diagnostics do not consult paid services."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.api.service import WorkflowService
from app.infrastructure.installation import installation_descriptor
from app.infrastructure.settings import Settings
from app.infrastructure.workflow_queue import InMemoryWorkflowQueue


def settings(root, **changes):
    return Settings(_env_file=None, **{
        "factory_mode_enabled": True, "factory_workspace_root": str(root),
        "workflow_queue_backend": "postgres", "checkpointer_backend": "postgres",
        "forgehand_revision": "revision-a", **changes,
    })


def test_shared_workspace_and_same_source_produce_same_identity(tmp_path):
    first = installation_descriptor(settings(tmp_path))
    second = installation_descriptor(settings(tmp_path))
    assert first == second and first["fingerprint"]
    assert len(list(tmp_path.glob(".forgehand-installation-id"))) == 1
    assert not list(tmp_path.glob(".installation-*"))


@pytest.mark.parametrize("change", [
    {"forgehand_revision": "revision-b"},
    {"factory_docker_socket": "/other/docker.sock"},
    {"factory_sandbox_network_enabled": True},
    {"factory_approved_scm_hosts_json": '["github.com", "git.example.test"]'},
    {"factory_repository_profiles_json": '{"acme/repo":"different"}'},
])
def test_deployment_configuration_changes_identity(tmp_path, change):
    original = settings(tmp_path)
    # model_copy isolates digest behavior; registry validity is tested by Settings.
    modified = original.model_copy(update=change)
    assert installation_descriptor(original)["fingerprint"] != installation_descriptor(modified)["fingerprint"]


def test_source_and_shared_volume_identity_both_affect_compatibility(tmp_path, monkeypatch):
    import app.infrastructure.installation as installation

    configured = settings(tmp_path)
    original = installation_descriptor(configured)["fingerprint"]
    monkeypatch.setattr(installation, "application_source_digest", lambda: "other-source")
    assert installation_descriptor(configured)["fingerprint"] != original
    before = installation_descriptor(configured)["fingerprint"]
    (tmp_path / ".forgehand-installation-id").write_text("00000000-0000-0000-0000-000000000001")
    assert installation_descriptor(configured)["fingerprint"] != before


def test_missing_revision_and_invalid_sentinel_fail_closed(tmp_path):
    missing = installation_descriptor(settings(tmp_path, forgehand_revision=""))
    assert missing["fingerprint"] is None and "revision_missing" in missing["errors"]
    (tmp_path / ".forgehand-installation-id").write_text("invalid")
    damaged = installation_descriptor(settings(tmp_path))
    assert damaged["fingerprint"] is None
    assert damaged["errors"] == ["workspace_identity_unavailable"]


@pytest.mark.asyncio
async def test_readiness_requires_expected_compatible_workers_and_hides_configuration(tmp_path):
    configured = settings(tmp_path, installation_expected_workers=2)
    queue = InMemoryWorkflowQueue()
    service = WorkflowService(Mock(), configured, queue, run_workers=False)
    fingerprint = installation_descriptor(configured)["fingerprint"]
    queue.configure_installation(None, required=False)
    await queue.touch_worker("legacy")
    queue.configure_installation("different", required=True)
    await queue.touch_worker("wrong-installation")
    assert not (await service.readiness())["ready"]
    queue.configure_installation(fingerprint, required=True)
    await queue.touch_worker("first")
    one = await service.readiness()
    assert not one["ready"] and one["compatible_workers"] == 1
    await queue.touch_worker("second")
    ready = await service.readiness()
    assert ready["ready"] and ready["compatible_workers"] == 2
    assert ready["incompatible_workers"] == 2
    assert str(tmp_path) not in json.dumps(ready)
    assert fingerprint not in json.dumps(ready)
    await service.shutdown()


@pytest.mark.asyncio
async def test_factory_off_preserves_legacy_heartbeat_readiness(tmp_path):
    configured = settings(tmp_path, factory_mode_enabled=False, forgehand_revision="")
    queue = InMemoryWorkflowQueue()
    service = WorkflowService(Mock(), configured, queue, run_workers=False)
    await queue.touch_worker("legacy")
    ready = await service.readiness()
    assert ready["ready"] and ready["installation_compatible"]
    assert not (tmp_path / ".forgehand-installation-id").exists()
    await service.shutdown()


@pytest.mark.asyncio
async def test_diagnostics_reports_absent_tools_without_secret_values(tmp_path, monkeypatch):
    import app.infrastructure.installation as installation

    monkeypatch.setattr(installation.shutil, "which", lambda name: None)
    monkeypatch.setattr(installation, "_docker_socket_accessible", lambda path: False)
    monkeypatch.setenv("GITHUB_TOKEN", "must-never-appear-in-report")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-never-appear-in-report")
    configured = settings(tmp_path, workflow_queue_backend="memory", checkpointer_backend="memory")
    service = WorkflowService(Mock(), configured, InMemoryWorkflowQueue(), run_workers=False)
    report = await service.installation_diagnostics()
    checks = {check["name"]: check for check in report["checks"]}
    assert report["schema_version"] == 1 and not report["ready"]
    assert checks["git"]["code"] == "git_unavailable"
    assert checks["docker_socket"]["code"] == "docker_socket_unavailable"
    assert checks["github_credentials"]["status"] == "pass"
    assert "must-never-appear-in-report" not in json.dumps(report)
    assert Path(report["configuration"]["workspace_root"]) == tmp_path
    await service.shutdown()
