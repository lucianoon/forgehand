"""Primitivas POSIX do sandbox e do runtime de workspace, com fronteira explícita.

O aplicativo precisa importar e servir o mission control em qualquer
plataforma: o perfil demo do README roda no Windows. Só a factory — lock de
arquivo por workflow, descritores abertos com O_NOFOLLOW, grupo de processos
dos comandos — exige um host POSIX, e ela falha fechada com PosixRequired
em vez de degradar em silêncio.

Todo uso de fcntl, killpg, getuid e das flags O_* do módulo os passa por aqui,
de modo que mypy em modo estrito fica limpo tanto no Linux quanto no Windows.
"""

from __future__ import annotations

import asyncio
import os
import sys

IS_POSIX = os.name == "posix"

# Fora do POSIX as flags não existem e viram 0. Elas só são combinadas depois
# de require_posix() nos caminhos que dependem delas para segurança; nos
# demais, a checagem S_ISREG após lstat continua valendo.
O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY: int = getattr(os, "O_DIRECTORY", 0)
O_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)


class PosixRequired(RuntimeError):
    """Recurso da factory invocado em plataforma sem os primitivos POSIX."""

    def __init__(self, feature: str) -> None:
        super().__init__(
            f"{feature} exige um host POSIX (Linux ou WSL); "
            f"plataforma atual: {sys.platform}"
        )
        self.feature = feature


def require_posix(feature: str) -> None:
    if not IS_POSIX:
        raise PosixRequired(feature)


def flock_exclusive_nonblocking(fd: int) -> None:
    """Trava exclusiva sem bloqueio; BlockingIOError quando outro dono já a tem."""
    require_posix("factory_workspace_lock")
    if sys.platform != "win32":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def kill_process_group(process: asyncio.subprocess.Process) -> None:
    """Mata o processo e, no POSIX, o grupo inteiro criado com start_new_session.

    Idempotente: processo já encerrado ou já colhido é ignorado.
    """
    if process.returncode is not None:
        return
    try:
        if sys.platform != "win32":
            import signal

            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def container_user() -> str:
    """uid:gid do controlador para `docker run --user`; nobody fora do POSIX."""
    if sys.platform != "win32":
        return f"{os.getuid() or 65534}:{os.getgid() or 65534}"
    return "65534:65534"
