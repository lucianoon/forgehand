"""A fronteira POSIX deixa os módulos importáveis em qualquer SO e falha só no uso."""

import importlib
import os

import pytest

from app.infrastructure import posix


def test_team_backup_imports_on_any_platform():
    module = importlib.import_module("app.operations.team_backup")
    assert callable(module.maintenance_lock)


@pytest.mark.skipif(os.name == "posix", reason="comportamento fora do POSIX")
def test_maintenance_lock_fails_closed_without_posix(tmp_path):
    from app.operations import team_backup

    with pytest.raises(posix.PosixRequired) as error:
        with team_backup.maintenance_lock(tmp_path):
            pass
    assert error.value.feature == "team_maintenance_lock"
    assert not (tmp_path / team_backup.LOCK_NAME).exists()


@pytest.mark.skipif(os.name != "posix", reason="flock exige POSIX")
def test_shared_locks_coexist_and_block_exclusive(tmp_path):
    path = tmp_path / "lock"
    first = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    second = os.open(path, os.O_RDWR)
    third = os.open(path, os.O_RDWR)
    try:
        posix.flock_nonblocking(first, shared=True)
        posix.flock_nonblocking(second, shared=True)
        with pytest.raises(BlockingIOError):
            posix.flock_nonblocking(third)
    finally:
        for fd in (first, second, third):
            os.close(fd)
