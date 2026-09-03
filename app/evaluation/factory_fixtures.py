"""Create a fresh, deterministic fixture Git repository; never reset user data."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def prepare_fixture(
    ecosystem: str, output_root: Path, fixture_root: Path
) -> tuple[Path, str]:
    if ecosystem not in {"python", "node"}:
        raise ValueError("unknown ecosystem")
    source = fixture_root / "fixtures" / ecosystem
    if not source.is_dir():
        raise ValueError("fixture source missing")
    destination = Path(
        tempfile.mkdtemp(prefix=f"forgehand-{ecosystem}-", dir=output_root)
    )
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
    )
    environment = {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.test",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.test",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args],
            cwd=destination,
            env=environment,
            text=True,
            stderr=subprocess.PIPE,
            timeout=30,
        ).strip()

    git("init", "-b", "main")
    git("add", ".")
    git("commit", "-m", f"fixture: {ecosystem} v1")
    return destination, git("rev-parse", "HEAD")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ecosystem", choices=["python", "node"], required=True)
    parser.add_argument("--output-root", type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument("--fixtures", type=Path, default=Path("benchmarks/factory"))
    args = parser.parse_args()
    path, sha = prepare_fixture(args.ecosystem, args.output_root, args.fixtures)
    print(json.dumps({"path": str(path), "base_sha": sha, "base_ref": "main"}))


if __name__ == "__main__":
    main()
