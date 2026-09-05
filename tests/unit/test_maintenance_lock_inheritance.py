import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest

from app.factory.lifecycle import inherited_lock_fds

pytestmark = pytest.mark.skipif(os.name != "posix", reason="factory mode requires POSIX")


@pytest.mark.parametrize("fault", ["missing_fd", "wrong_file", "symlink", "closed_fd"])
def test_invalid_maintenance_descriptor_fails_closed(tmp_path, monkeypatch, fault):
    path = tmp_path / "lock"
    path.touch()
    other = tmp_path / "other"
    other.touch()
    fd = os.open(path, os.O_RDONLY)
    try:
        monkeypatch.setenv("FORGEHAND_MAINTENANCE_FD", str(fd))
        monkeypatch.setenv("FORGEHAND_MAINTENANCE_LOCK_PATH", str(path))
        if fault == "missing_fd":
            monkeypatch.delenv("FORGEHAND_MAINTENANCE_FD")
        elif fault == "wrong_file":
            monkeypatch.setenv("FORGEHAND_MAINTENANCE_LOCK_PATH", str(other))
        elif fault == "symlink":
            path.unlink()
            path.symlink_to(other)
        else:
            os.close(fd)
        with pytest.raises(ValueError, match="installation_maintenance_lock_invalid"):
            inherited_lock_fds()
    finally:
        if fault != "closed_fd":
            os.close(fd)


async def test_git_child_keeps_maintenance_lock_after_worker_is_killed(tmp_path):
    import fcntl

    lock = tmp_path / ".maintenance.lock"
    marker = tmp_path / "child.pid"
    script = r'''
import asyncio, fcntl, os, sys
from pathlib import Path
from app.factory.workspace import SafeGitRunner
root = Path(sys.argv[1])
lock = root / '.maintenance.lock'
fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
os.environ['FORGEHAND_MAINTENANCE_FD'] = str(fd)
os.environ['FORGEHAND_MAINTENANCE_LOCK_PATH'] = str(lock)
runner = SafeGitRunner(root, git_executable=sys.executable)
asyncio.run(runner.run(['-c', "import os,time,pathlib;pathlib.Path('child.pid').write_text(str(os.getpid()));time.sleep(60)"]))
'''
    worker = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    child = None
    try:
        async with asyncio.timeout(5):
            while not marker.exists():
                if worker.poll() is not None:
                    pytest.fail("worker did not start the Git subprocess")
                await asyncio.sleep(0.01)
        child = int(marker.read_text())
        worker.kill()
        worker.wait(timeout=5)
        with lock.open("a") as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.killpg(child, signal.SIGKILL)
            child = None
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.01)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        if child is not None:
            os.killpg(child, signal.SIGKILL)
        if worker.stderr is not None:
            worker.stderr.close()
