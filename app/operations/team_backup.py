"""Offline PostgreSQL and filesystem backup for the managed team installation."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import uuid
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Any, IO, Iterator

FORMAT_VERSION = 1
LOCK_NAME = ".maintenance.lock"
RESTORE_MARKER = ".restore-in-progress"
RESTORE_INFO = ".restore-info.json"
RESERVED = {LOCK_NAME, RESTORE_MARKER, RESTORE_INFO}


class BackupError(RuntimeError):
    """Safe operator diagnostic; never include database connection strings."""


def _data_root(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink() or path != path.resolve():
        raise BackupError("Data root must be an absolute path without symbolic components.")
    return path


def _write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _stream_sha256(handle: IO[bytes]) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return _stream_sha256(handle)


@contextmanager
def maintenance_lock(root: Path, *, shared: bool = False) -> Iterator[int]:
    """Nonblocking lock shared by managed runtimes and exclusive maintenance."""
    if not root.is_dir() or root.is_symlink():
        raise BackupError("Data root must be an existing ordinary directory.")
    fd = os.open(root / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BackupError("Invalid maintenance lock.")
        os.fchmod(fd, 0o600)
        if os.geteuid() == 0:
            owner = root.stat()
            os.fchown(fd, owner.st_uid, owner.st_gid)
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("API, worker, child process or maintenance is still active.") from None
        yield fd
    finally:
        os.close(fd)


def run_managed(root: Path, command: list[str]) -> None:
    """Replace the launcher; the application and inherited children retain its lock."""
    root = _data_root(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not command:
        raise BackupError("A runtime command is required after --.")
    with maintenance_lock(root, shared=True) as fd:
        if (root / RESTORE_MARKER).exists():
            raise BackupError("Restore is incomplete; the runtime remains stopped.")
        if (root / RESTORE_INFO).exists():
            info = json.loads((root / RESTORE_INFO).read_text())
            if str(root) != info.get("required_runtime_data_root"):
                raise BackupError("This offline restore must use its original absolute data path before startup.")
        os.set_inheritable(fd, True)
        environment = dict(os.environ)
        environment["FORGEHAND_MAINTENANCE_FD"] = str(fd)
        environment["FORGEHAND_MAINTENANCE_LOCK_PATH"] = str(root / LOCK_NAME)
        os.execvpe(command[0], command, environment)


def _database(dsn: str, *, empty: bool = False) -> dict[str, Any]:
    try:
        import psycopg

        _pg_environment(dsn)  # The inspection and tools must target the same explicit connection.
        with psycopg.connect(dsn, autocommit=True, application_name="forgehand-maintenance") as db:
            identity = db.execute("SELECT current_database(), current_setting('server_version_num')").fetchone()
            if identity is None:
                raise BackupError("Database identity is unavailable.")
            name, server_version = identity
            active = db.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                "AND pid<>pg_backend_pid() AND backend_type='client backend'"
            ).fetchone()
            if active is None or active[0]:
                raise BackupError("Database has other client sessions; stop API/workers and administrative clients first.")
            if empty:
                populated = db.execute("""
                    SELECT EXISTS (
                      SELECT 1 FROM pg_namespace WHERE nspname <> 'public'
                        AND nspname <> 'information_schema' AND nspname NOT LIKE 'pg_%'
                      UNION ALL
                      SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                        WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg_%'
                      UNION ALL
                      SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                        WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg_%'
                      UNION ALL
                      SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
                        WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg_%'
                    )
                """).fetchone()
                if populated is None or populated[0]:
                    raise BackupError("Restore database must be newly created and empty.")
            return {"name": name, "server_version": int(server_version)}
    except BackupError:
        raise
    except Exception:
        raise BackupError("Database inspection failed; check the connection and privileges locally.") from None


def _pg_environment(dsn: str) -> dict[str, str]:
    from psycopg.conninfo import conninfo_to_dict

    try:
        parameters = {key: str(value) for key, value in conninfo_to_dict(dsn).items() if value is not None}
    except Exception:
        raise BackupError("Invalid database connection configuration.") from None
    allowed = {
        "host": "PGHOST", "hostaddr": "PGHOSTADDR", "port": "PGPORT",
        "dbname": "PGDATABASE", "user": "PGUSER", "password": "PGPASSWORD",
        "sslmode": "PGSSLMODE", "sslcert": "PGSSLCERT", "sslkey": "PGSSLKEY",
        "sslrootcert": "PGSSLROOTCERT", "sslcrl": "PGSSLCRL", "sslcrldir": "PGSSLCRLDIR",
        "connect_timeout": "PGCONNECT_TIMEOUT", "options": "PGOPTIONS",
        "channel_binding": "PGCHANNELBINDING", "passfile": "PGPASSFILE",
        "target_session_attrs": "PGTARGETSESSIONATTRS",
    }
    if (not all(parameters.get(key) for key in ("dbname", "user", "port"))
            or not (parameters.get("host") or parameters.get("hostaddr"))
            or set(parameters) - set(allowed)):
        raise BackupError("Use explicit host, port, user and database with supported libpq parameters.")
    if ("=" in parameters["dbname"] or parameters["dbname"].startswith(("postgresql://", "postgres://"))
            or any("," in parameters.get(key, "") for key in ("host", "hostaddr", "port"))
            or not parameters["port"].isdigit() or not 0 < int(parameters["port"]) < 65536):
        raise BackupError("Use a single PostgreSQL endpoint and an ordinary database name.")
    if any(key.startswith("PG") and value for key, value in os.environ.items()):
        raise BackupError("Clear inherited PG* variables; provide the complete connection in the selected URL variable.")
    environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "HOME"}}
    environment.update({allowed[key]: str(value) for key, value in parameters.items() if value is not None})
    environment["PGAPPNAME"] = "forgehand-backup-tool"
    return environment


def _pg_tool(executable: str, arguments: list[str], dsn: str, *, output: Any = None) -> None:
    try:
        result = subprocess.run(
            [executable, *arguments], env=_pg_environment(dsn),
            stdin=subprocess.DEVNULL, stdout=output or subprocess.DEVNULL,
            stderr=subprocess.PIPE, check=False,
        )
    except OSError:
        raise BackupError("PostgreSQL client executable is unavailable.") from None
    if result.returncode:
        raise BackupError(f"PostgreSQL client failed with exit code {result.returncode}; no server response was exported.")


@contextmanager
def _idle_factory(root: Path) -> Iterator[None]:
    with ExitStack() as stack:
        control = root / "factory" / "control"
        if control.exists():
            for path in sorted(control.glob("*.lock")):
                fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
                stack.callback(os.close, fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise BackupError("A factory child still holds a workspace lock.") from None
            journal = control / "lifecycle.sqlite3"
            if journal.exists():
                with closing(sqlite3.connect(f"file:{journal}?mode=ro", uri=True)) as db:
                    if db.execute("SELECT count(*) FROM containers").fetchone()[0]:
                        raise BackupError("Sandbox cleanup is pending in the factory journal.")
        yield


def _excluded(path: PurePosixPath) -> bool:
    return bool(path.parts) and path.parts[0] in RESERVED


def _safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or str(path) != name or "\\" in name
            or any(part in {"", ".", ".."} for part in path.parts) or _excluded(path)):
        raise BackupError("Archive contains an unsafe or reserved path.")
    return path


def _check_links(entries: list[dict[str, Any]]) -> None:
    inventory = {item["path"]: item for item in entries}
    links = {item["path"]: item["link"] for item in entries if item["kind"] == "symlink"}
    for item in entries:
        parts = PurePosixPath(item["path"]).parts
        if any(inventory.get("/".join(parts[:index]), {}).get("kind") != "directory"
               for index in range(1, len(parts))):
            raise BackupError("Archive member parent is absent or is not a directory.")
    for name, target in links.items():
        if not target or target.startswith("/") or "\\" in target:
            raise BackupError("Archive symlink escapes the data root.")
        pending = [*PurePosixPath(name).parent.parts, *target.split("/")]
        resolved: list[str] = []
        hops = 0
        while pending:
            part = pending.pop(0)
            if part in {"", "."}:
                continue
            if part == "..":
                if not resolved:
                    raise BackupError("Archive symlink escapes the data root.")
                resolved.pop()
                continue
            candidate = "/".join([*resolved, part])
            if candidate in links:
                hops += 1
                if hops > 40:
                    raise BackupError("Archive contains cyclic symbolic links.")
                pending = links[candidate].split("/") + pending
            else:
                resolved.append(part)


def _archive_data(root: Path, destination: Path) -> list[dict[str, Any]]:
    entries = []
    with destination.open("xb") as output:
        os.chmod(destination, 0o600)
        with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
            for directory, dirs, files in os.walk(root, followlinks=False):
                dirs.sort()
                files.sort()
                for name in [*dirs, *files]:
                    path = Path(directory) / name
                    relative = PurePosixPath(path.relative_to(root).as_posix())
                    if _excluded(relative):
                        if name in dirs:
                            dirs.remove(name)
                        continue
                    info = path.lstat()
                    member = archive.gettarinfo(str(path), arcname=str(relative))
                    entry: dict[str, Any] = {"path": str(relative), "mode": stat.S_IMODE(info.st_mode)}
                    if member.isdir():
                        entry["kind"] = "directory"
                        archive.addfile(member)
                    elif member.issym():
                        entry.update(kind="symlink", link=member.linkname)
                        archive.addfile(member)
                    elif stat.S_ISREG(info.st_mode):
                        member.type = tarfile.REGTYPE
                        member.linkname = ""
                        member.size = info.st_size
                        entry.update(kind="file", size=info.st_size, sha256=_sha256(path))
                        with path.open("rb") as source:
                            archive.addfile(member, source)
                    else:
                        raise BackupError("Data root contains a socket, device or unsupported special file.")
                    entries.append(entry)
    _check_links(entries)
    return entries


def _application() -> dict[str, str]:
    try:
        application_version = version("forgehand")
    except PackageNotFoundError:
        application_version = "unknown"
    revision = os.environ.get("FORGEHAND_REVISION", "")
    if len(revision) not in {40, 64} or any(char not in "0123456789abcdef" for char in revision):
        try:
            result = subprocess.run(
                ["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=False,
            )
            revision = result.stdout.strip() if result.returncode == 0 else "unknown"
        except OSError:
            revision = "unknown"
    return {"version": application_version, "revision": revision}


def backup(dsn: str, root: Path, destination: Path, *, pg_dump: str = "pg_dump") -> dict[str, Any]:
    root, destination = _data_root(root), destination.resolve()
    if destination.exists() or destination.is_symlink() or destination.is_relative_to(root):
        raise BackupError("Backup output must be a new directory outside the data root.")
    if not destination.parent.is_dir():
        raise BackupError("Create the backup parent directory first.")
    with maintenance_lock(root), _idle_factory(root):
        if (root / RESTORE_MARKER).exists():
            raise BackupError("Cannot back up an incomplete restore.")
        source = _database(dsn)
        metadata = root.stat()
        destination.mkdir(mode=0o700)
        try:
            dump = destination / "database.dump"
            with dump.open("xb") as output:
                os.chmod(dump, 0o600)
                _pg_tool(pg_dump, ["--format=custom", "--no-owner", "--no-privileges"], dsn, output=output)
            files = destination / "files.tar.gz"
            entries = _archive_data(root, files)
            _database(dsn)
            manifest = {
                "format_version": FORMAT_VERSION,
                "backup_id": str(uuid.uuid4()),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "application": _application(),
                "source": {"database": source["name"], "server_version": source["server_version"],
                           "data_root": str(root), "uid": metadata.st_uid, "gid": metadata.st_gid},
                "artifacts": {name: {"sha256": _sha256(destination / name),
                                     "size": (destination / name).stat().st_size}
                              for name in ("database.dump", "files.tar.gz")},
                "entries": entries,
            }
            _write_json(destination / "manifest.json", manifest)
            return manifest
        except Exception:
            # Preserve partial artifacts for diagnosis, but no manifest means invalid.
            raise


def validate_backup(bundle: Path) -> dict[str, Any]:
    bundle = bundle.resolve()
    try:
        manifest_file = bundle / "manifest.json"
        if manifest_file.is_symlink() or manifest_file.stat().st_size > 50_000_000:
            raise BackupError("Invalid backup manifest.")
        manifest: dict[str, Any] = json.loads(manifest_file.read_text())
        if manifest["format_version"] != FORMAT_VERSION:
            raise BackupError("Unsupported backup format version.")
        source = manifest["source"]
        if (not isinstance(source["database"], str) or not source["database"]
                or not Path(source["data_root"]).is_absolute()
                or type(source["uid"]) is not int or source["uid"] < 0
                or type(source["gid"]) is not int or source["gid"] < 0):
            raise BackupError("Invalid backup source identity.")
        if set(manifest["artifacts"]) != {"database.dump", "files.tar.gz"}:
            raise BackupError("Invalid backup artifact inventory.")
        for name, expected in manifest["artifacts"].items():
            path = bundle / name
            if (path.is_symlink() or not path.is_file() or path.stat().st_size != expected["size"]
                    or _sha256(path) != expected["sha256"]):
                raise BackupError("Backup artifact checksum mismatch.")
        entries = manifest["entries"]
        inventory = {entry["path"]: entry for entry in entries}
        if len(inventory) != len(entries):
            raise BackupError("Duplicate archive paths.")
        for name in inventory:
            _safe_name(name)
        actual = set()
        with tarfile.open(bundle / "files.tar.gz", "r:gz") as archive:
            for member in archive:
                _safe_name(member.name)
                if member.name in actual or member.name not in inventory:
                    raise BackupError("Archive inventory differs from manifest.")
                actual.add(member.name)
                expected = inventory[member.name]
                if member.mode != expected["mode"]:
                    raise BackupError("Archive metadata differs from manifest.")
                if member.isfile() and expected["kind"] == "file":
                    content = archive.extractfile(member)
                    if content is None:
                        raise BackupError("Archive file is missing.")
                    digest = _stream_sha256(content)
                    if member.size != expected["size"] or digest != expected["sha256"]:
                        raise BackupError("Archive file checksum mismatch.")
                elif member.isdir() and expected["kind"] == "directory":
                    pass
                elif member.issym() and expected["kind"] == "symlink" and member.linkname == expected["link"]:
                    pass
                else:
                    raise BackupError("Unsupported archive member or mismatched type.")
        if actual != set(inventory):
            raise BackupError("Archive inventory is incomplete.")
        _check_links(entries)
        return manifest
    except BackupError:
        raise
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError):
        raise BackupError("Backup is malformed or incomplete.") from None


def restore(
    dsn: str, root: Path, bundle: Path, *, pg_restore: str = "pg_restore", original_path: bool = False,
) -> dict[str, Any]:
    manifest = validate_backup(bundle)
    root, bundle = _data_root(root), bundle.resolve()
    if root.exists() or root.is_symlink() or not root.parent.is_dir():
        raise BackupError("Restore data root must not exist; its parent must already exist.")
    source = manifest["source"]
    if str(root) == source["data_root"] and not original_path:
        raise BackupError("Restoring the original path requires --original-path on an isolated offline host.")
    if root.is_relative_to(bundle) or bundle.is_relative_to(root):
        raise BackupError("Restore data root and backup bundle must be separate.")
    if os.geteuid() not in {0, source["uid"]}:
        raise BackupError("Restore must run as the recorded data owner or root.")
    target = _database(dsn, empty=True)
    if target["name"] == source["database"]:
        raise BackupError("Restore requires a different database name from the source.")
    _pg_tool(pg_restore, ["--list", str(bundle / "database.dump")], dsn)
    root.mkdir(mode=0o700)
    with maintenance_lock(root):
        _write_json(root / RESTORE_MARKER, {"backup_id": manifest["backup_id"]})
        # Names, types, links and content hashes were checked before destination mutation.
        with tarfile.open(bundle / "files.tar.gz", "r:gz") as archive:
            members = list(archive)
            for member in sorted(members, key=lambda item: (item.name.count("/"), item.name)):
                path = root / member.name
                if member.isdir():
                    path.mkdir(mode=0o700, parents=True, exist_ok=True)
                elif member.isfile():
                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    content = archive.extractfile(member)
                    if content is None:
                        raise BackupError("Archive file is missing.")
                    with path.open("xb") as output:
                        shutil.copyfileobj(content, output)
                    os.chmod(path, 0o700 if member.mode & 0o111 else 0o600)
            # No descendant member can traverse these links; create them last.
            for member in members:
                if member.issym():
                    path = root / member.name
                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    path.symlink_to(member.linkname)
        _pg_tool(pg_restore, ["--single-transaction", "--exit-on-error", "--no-owner", "--no-privileges",
                              "--dbname", target["name"], str(bundle / "database.dump")], dsn)
        _write_json(root / RESTORE_INFO, {"backup_id": manifest["backup_id"],
                                        "required_runtime_data_root": source["data_root"],
                                        "restored_database": target["name"]})
        if os.geteuid() == 0:
            for directory, dirs, files in os.walk(root, followlinks=False):
                for name in [*dirs, *files]:
                    os.chown(Path(directory) / name, source["uid"], source["gid"], follow_symlinks=False)
            os.chown(root, source["uid"], source["gid"])
        (root / RESTORE_MARKER).unlink()
    return {"backup_id": manifest["backup_id"], "database": target["name"], "data_root": str(root),
            "required_runtime_data_root": source["data_root"],
            "runtime_path_matches": str(root) == source["data_root"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline maintenance for a managed Forgehand team installation")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="hold the shared installation lock while running API/worker")
    run.add_argument("--data-root", type=Path, required=True)
    run.add_argument("runtime", nargs=argparse.REMAINDER)
    create = sub.add_parser("backup", help="back up stopped runtimes and PostgreSQL")
    create.add_argument("--data-root", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--database-url-env", default="DATABASE_URL")
    create.add_argument("--pg-dump", default="pg_dump")
    recover = sub.add_parser("restore", help="restore into a new empty database and new data root")
    recover.add_argument("--data-root", type=Path, required=True)
    recover.add_argument("--backup", type=Path, required=True)
    recover.add_argument("--database-url-env", default="RESTORE_DATABASE_URL")
    recover.add_argument("--pg-restore", default="pg_restore")
    recover.add_argument("--original-path", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            command = args.runtime[1:] if args.runtime[:1] == ["--"] else args.runtime
            run_managed(args.data_root, command)
            return 0
        dsn = os.environ.get(args.database_url_env)
        if not dsn:
            raise BackupError("Set the database connection in the named environment variable.")
        if args.command == "backup":
            manifest = backup(dsn, args.data_root, args.output, pg_dump=args.pg_dump)
            result = {"backup_id": manifest["backup_id"], "output": str(args.output),
                      "files": len(manifest["entries"])}
        else:
            result = restore(dsn, args.data_root, args.backup, pg_restore=args.pg_restore,
                             original_path=args.original_path)
        print(json.dumps(result))
        return 0
    except BackupError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception:
        print("Maintenance failed; partial restore remains stopped. Inspect local resources before retrying.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
