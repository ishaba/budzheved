"""Исключительный замок на файл: fcntl.flock на Unix, msvcrt.locking на Windows."""

from __future__ import annotations

import contextlib
import errno
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

# Замок занят другим процессом: msvcrt.locking отвечает EDEADLK (LK_LOCK после ~10 с
# попыток) или EACCES, а Windows — ERROR_LOCK_VIOLATION (33) или ERROR_SHARING_BUFFER_EXCEEDED (36).
_TAKEN_ERRNO = {errno.EDEADLK, errno.EACCES}
_TAKEN_WINERROR = {33, 36}


def retry_while_taken(lock_once: Callable[[], None]) -> None:
    """Повторять попытку, пока замок занят; любую другую ошибку поднять сразу."""
    while True:
        try:
            lock_once()
            return
        except OSError as exc:
            taken = exc.errno in _TAKEN_ERRNO or getattr(exc, "winerror", None) in _TAKEN_WINERROR
            if not taken:
                raise


if sys.platform == "win32":
    import msvcrt

    def _acquire(fh) -> None:
        # LK_LOCK сам повторяет попытку около 10 с и потом бросает OSError: ждём в цикле.
        fh.seek(0)
        retry_while_taken(lambda: msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1))

    def _release(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _acquire(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_EX)

    def _release(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)


@contextlib.contextmanager
def exclusive(path: Path) -> Iterator[None]:
    """Держит замок на файле `path`, пока выполняется блок; другой процесс ждёт.

    Файл создаётся, если его нет, и не усекается. Если процесс убит, система снимает замок.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # «a», а не «w»: создаёт файл и не усекает его, пока им владеет другой процесс.
    with path.open("a", encoding="utf-8") as fh:
        _acquire(fh)
        try:
            yield
        finally:
            _release(fh)
