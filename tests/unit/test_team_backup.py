"""Offline backup safety; PostgreSQL round-trip lives in the opt-in integration test."""

import hashlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from app.factory.lifecycle import WorkspaceJournal
from app.operations import team_backup as operation

pytestmark = pytest.mark.skipif(os.name != "posix", reason="team runtime uses POSIX locks")


@pytest.fixture
def fake_database(monkeypatch):
    calls = []

    def database(dsn, *, empty=False):
        calls.append(("inspect", dsn, empty))
        return {"name": dsn, "server_version": 160000}

    def pg_tool(executable, arguments, dsn, *, output=None):
        calls.append(("tool", executable, tuple(arguments), dsn))
        if output:
            output.write(b"PGDMP-fixture")

    monkeypatch.setattr(operation, "_database", database)
    monkeypatch.setattr(operation, "_pg_tool", pg_tool)
    monkeypatch.setattr(operation, "_application", lambda: {"version": "test", "revision": "a" * 40})
    return calls


def source_data(tmp_path):
    root = tmp_path / "source"
    root.mkdir(mode=0o700)
    WorkspaceJournal(root / "factory" / "control")
    (root / "audit").mkdir()
    (root / "audit" / "api.jsonl").write_text('{"owner":"team-owner","workflow":"wf-1"}\n')
    (root / "script.sh").write_text("#!/bin/sh\nprintf 'ok\\n'\n")
    (root / "script.sh").chmod(0o755)
    (root / ".env").write_text("GITHUB_TOKEN=fixture-secret\n")
    (root / ".env.production").write_text("TOKEN=fixture-secret\n")
    (root / "credentials.env").write_text("TOKEN=fixture-secret\n")
    return root


def bundle(tmp_path, fake_database):
    root = source_data(tmp_path)
    output = tmp_path / "backup"
    manifest = operation.backup("source-db", root, output)
    return root, output, manifest


def test_backup_and_offline_restore_preserve_files_and_owner_metadata(tmp_path, fake_database):
    root = source_data(tmp_path)
    (root / "audit-link").symlink_to("audit")
    os.link(root / "audit" / "api.jsonl", root / "audit-copy.jsonl")
    output = tmp_path / "backup"
    manifest = operation.backup("source-db", root, output)
    assert operation.validate_backup(output) == manifest
    assert manifest["source"]["uid"] == root.stat().st_uid
    assert manifest["application"]["revision"] == "a" * 40
    assert {".env", ".env.production", "credentials.env"} <= {entry["path"] for entry in manifest["entries"]}
    assert "fixture-secret" not in json.dumps(manifest)
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output.iterdir())

    restored = tmp_path / "restored"
    receipt = operation.restore("restored-db", restored, output)
    assert receipt["required_runtime_data_root"] == str(root)
    assert not receipt["runtime_path_matches"]
    assert (restored / "audit" / "api.jsonl").read_bytes() == (root / "audit" / "api.jsonl").read_bytes()
    assert (restored / "audit-copy.jsonl").read_bytes() == (root / "audit-copy.jsonl").read_bytes()
    assert (restored / "audit-link").is_symlink()
    assert stat.S_IMODE((restored / "script.sh").stat().st_mode) == 0o700
    assert (restored / ".env").read_bytes() == (root / ".env").read_bytes()
    assert (restored / "credentials.env").read_bytes() == (root / "credentials.env").read_bytes()
    with pytest.raises(operation.BackupError, match="original absolute"):
        operation.run_managed(restored, [sys.executable, "-c", "raise AssertionError"])


def test_original_path_requires_opt_in_and_an_absent_directory(tmp_path, fake_database):
    root, output, _ = bundle(tmp_path, fake_database)
    retained = tmp_path / "retained-source"
    root.rename(retained)
    with pytest.raises(operation.BackupError, match="--original-path"):
        operation.restore("restored-db", root, output)
    assert not root.exists()
    result = operation.restore("restored-db", root, output, original_path=True)
    assert result["runtime_path_matches"]
    assert (retained / "audit" / "api.jsonl").read_bytes() == (root / "audit" / "api.jsonl").read_bytes()
    with pytest.raises(operation.BackupError, match="must not exist"):
        operation.restore("another-db", root, output, original_path=True)


def test_restore_refuses_source_database_before_creating_data_root(tmp_path, fake_database):
    _, output, _ = bundle(tmp_path, fake_database)
    target = tmp_path / "restored"
    fake_database.clear()
    with pytest.raises(operation.BackupError, match="different database"):
        operation.restore("source-db", target, output)
    assert not target.exists()
    assert not any(call[0] == "tool" for call in fake_database)


@pytest.mark.parametrize("artifact", ["database.dump", "files.tar.gz"])
def test_checksums_are_validated_before_database_or_filesystem_mutation(tmp_path, fake_database, artifact):
    _, output, _ = bundle(tmp_path, fake_database)
    with (output / artifact).open("ab") as handle:
        handle.write(b"corrupt")
    fake_database.clear()
    target = tmp_path / "restored"
    with pytest.raises(operation.BackupError, match="checksum"):
        operation.restore("restored-db", target, output)
    assert not target.exists() and not fake_database


def replace_archive(output, specifications):
    manifest = json.loads((output / "manifest.json").read_text())
    entries = []
    with tarfile.open(output / "files.tar.gz", "w:gz") as archive:
        for name, kind, value in specifications:
            info = tarfile.TarInfo(name)
            info.mode = 0o600
            entry = {"path": name, "kind": kind, "mode": info.mode}
            if kind == "file":
                data = value.encode()
                info.size = len(data)
                entry.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
                archive.addfile(info, io.BytesIO(data))
            elif kind == "directory":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.type = tarfile.SYMTYPE
                info.linkname = value
                entry["link"] = value
                archive.addfile(info)
            entries.append(entry)
    manifest["entries"] = entries
    path = output / "files.tar.gz"
    manifest["artifacts"]["files.tar.gz"] = {"size": path.stat().st_size, "sha256": operation._sha256(path)}
    (output / "manifest.json").write_text(json.dumps(manifest))


@pytest.mark.parametrize("specifications", [
    [("../escape", "file", "bad")],
    [("/absolute", "file", "bad")],
    [(".restore-info.json", "file", "bad")],
    [("escape", "symlink", "../outside")],
    [("escape", "symlink", "/outside")],
    [("escape", "symlink", "safe"), ("escape/file", "file", "bad")],
    [("file", "file", "ok"), ("file/nested", "file", "bad")],
    [("duplicate", "file", "a"), ("duplicate", "file", "b")],
    [("dir", "directory", ""), ("dir/link", "symlink", ".."),
     ("escape", "symlink", "dir/link/../outside")],
])
def test_archive_paths_and_links_validated_before_destination_mutation(tmp_path, fake_database, specifications):
    _, output, _ = bundle(tmp_path, fake_database)
    replace_archive(output, specifications)
    fake_database.clear()
    target = tmp_path / "restored"
    with pytest.raises(operation.BackupError):
        operation.restore("restored-db", target, output)
    assert not target.exists() and not fake_database
    assert not (tmp_path / "escape").exists()


def test_running_managed_process_prevents_backup_before_database_access(tmp_path, fake_database):
    root = source_data(tmp_path)
    started = root / "started"
    process = subprocess.Popen([
        sys.executable, "-m", "app.operations.team_backup", "run", "--data-root", str(root), "--",
        sys.executable, "-c", "import pathlib,sys,time;pathlib.Path(sys.argv[1]).touch();time.sleep(30)",
        str(started),
    ], start_new_session=True)
    try:
        deadline = time.monotonic() + 3
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        with pytest.raises(operation.BackupError, match="still active"):
            operation.backup("source-db", root, tmp_path / "denied")
        assert not fake_database
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=3)
    operation.backup("source-db", root, tmp_path / "allowed")


def test_busy_workspace_or_orphan_sandbox_refuses_backup(tmp_path, fake_database):
    root = source_data(tmp_path)
    journal = WorkspaceJournal(root / "factory" / "control")
    with journal.exclusive("wf-1"):
        with pytest.raises(operation.BackupError, match="workspace lock"):
            operation.backup("source-db", root, tmp_path / "denied-lock")
    journal.record_container("wf-1", "sandbox-fixture", "ownership-token-fixture")
    with pytest.raises(operation.BackupError, match="Sandbox cleanup"):
        operation.backup("source-db", root, tmp_path / "denied-container")
    assert not fake_database


def test_restore_failure_keeps_runtime_blocked(tmp_path, fake_database, monkeypatch):
    _, output, _ = bundle(tmp_path, fake_database)
    original = operation._pg_tool

    def failing_restore(executable, arguments, dsn, **kwargs):
        if "--dbname" in arguments:
            raise operation.BackupError("Injected restore failure")
        return original(executable, arguments, dsn, **kwargs)

    monkeypatch.setattr(operation, "_pg_tool", failing_restore)
    target = tmp_path / "restored"
    with pytest.raises(operation.BackupError, match="Injected"):
        operation.restore("restored-db", target, output)
    assert (target / operation.RESTORE_MARKER).exists()
    with pytest.raises(operation.BackupError, match="incomplete"):
        operation.run_managed(target, [sys.executable, "-c", "raise AssertionError"])


def test_relative_and_symlink_data_roots_are_rejected(tmp_path):
    with pytest.raises(operation.BackupError, match="absolute"):
        operation.run_managed(Path("relative"), ["false"])
    target = tmp_path / "data"
    target.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(target)
    with pytest.raises(operation.BackupError, match="absolute"):
        operation.run_managed(link, ["false"])


def test_cli_errors_do_not_export_database_secrets(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:fixture-secret@localhost/source")
    monkeypatch.setattr(operation, "_database", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fixture-secret")))
    root = source_data(tmp_path)
    result = operation.main(["backup", "--data-root", str(root), "--output", str(tmp_path / "denied")])
    captured = capsys.readouterr()
    assert result == 2 and "fixture-secret" not in captured.out + captured.err


def test_postgres_client_uses_private_environment_without_connection_in_arguments(monkeypatch):
    calls = []
    monkeypatch.setenv("GITHUB_TOKEN", "unrelated-provider-secret")

    def execute(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(operation.subprocess, "run", execute)
    dsn = "postgresql://owner:fixture-secret@localhost:5432/source?sslmode=require"
    operation._pg_tool("pg_dump", ["--format=custom"], dsn)
    arguments, settings = calls[0]
    assert arguments == ["pg_dump", "--format=custom"]
    assert dsn not in repr(arguments) and "fixture-secret" not in repr(arguments)
    assert settings["env"]["PGPASSWORD"] == "fixture-secret"
    assert settings["env"]["PGDATABASE"] == "source"
    assert settings["env"]["PGSSLMODE"] == "require"
    assert "GITHUB_TOKEN" not in settings["env"] and "PGSERVICE" not in settings["env"]
    assert os.environ["GITHUB_TOKEN"] == "unrelated-provider-secret"


def test_postgres_failure_never_includes_server_stderr(monkeypatch):
    monkeypatch.setattr(operation.subprocess, "run", lambda *a, **k: type(
        "Result", (), {"returncode": 1, "stderr": b"password=fixture-secret"}
    )())
    with pytest.raises(operation.BackupError) as result:
        operation._pg_tool("pg_dump", [], "postgresql://owner:fixture-secret@localhost:5432/source")
    assert "fixture-secret" not in str(result.value)


def test_image_revision_is_recorded_without_requiring_a_git_checkout(monkeypatch):
    monkeypatch.setenv("FORGEHAND_REVISION", "d" * 40)
    monkeypatch.setattr(operation.subprocess, "run", lambda *a, **k: pytest.fail("image revision requires no Git process"))
    assert operation._application()["revision"] == "d" * 40


@pytest.mark.parametrize("dsn", ["dbname=source user=owner", "postgresql:///source", "postgresql://owner@localhost/source"])
def test_ambiguous_libpq_defaults_are_rejected(dsn):
    with pytest.raises(operation.BackupError, match="explicit host"):
        operation._pg_environment(dsn)


def test_inherited_libpq_settings_cannot_redirect_inspection_or_dump(monkeypatch):
    monkeypatch.setenv("PGSERVICE", "other-server")
    with pytest.raises(operation.BackupError, match="Clear inherited"):
        operation._pg_environment("postgresql://owner@localhost:5432/source")


def test_ownership_failure_preserves_incomplete_restore_marker(tmp_path, fake_database, monkeypatch):
    _, output, _ = bundle(tmp_path, fake_database)
    monkeypatch.setattr(operation.os, "geteuid", lambda: 0)
    monkeypatch.setattr(operation.os, "fchown", lambda *a: None)
    monkeypatch.setattr(operation.os, "chown", lambda *a, **k: (_ for _ in ()).throw(PermissionError("chown failed")))
    target = tmp_path / "restored"
    with pytest.raises(PermissionError):
        operation.restore("restored-db", target, output)
    assert (target / operation.RESTORE_MARKER).exists()
    with pytest.raises(operation.BackupError, match="incomplete"):
        operation.run_managed(target, [sys.executable, "-c", "raise AssertionError"])


@pytest.mark.parametrize("dsn", [
    "host=localhost port=5432 user=owner dbname='dbname=other'",
    "host=localhost port=5432 user=owner dbname='postgresql://other/production'",
    "host=first,second port=5432 user=owner dbname=source",
    "host=localhost port=5432,5433 user=owner dbname=source",
])
def test_connection_string_database_names_and_failover_endpoints_are_rejected(dsn):
    with pytest.raises(operation.BackupError, match="single PostgreSQL endpoint"):
        operation._pg_environment(dsn)


def test_only_root_maintenance_metadata_is_excluded(tmp_path, fake_database):
    root = source_data(tmp_path)
    project = root / "factory" / "project"
    project.mkdir()
    for name in operation.RESERVED | {".env", ".env.example"}:
        (project / name).write_text("versioned project data")
    output = tmp_path / "backup"
    manifest = operation.backup("source-db", root, output)
    paths = {entry["path"] for entry in manifest["entries"]}
    assert not operation.RESERVED & paths
    assert all("factory/project/" + name in paths for name in operation.RESERVED | {".env", ".env.example"})
    restored = tmp_path / "restored"
    operation.restore("restored-db", restored, output)
    for name in operation.RESERVED | {".env", ".env.example"}:
        assert (restored / "factory" / "project" / name).read_text() == "versioned project data"
