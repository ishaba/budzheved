"""Копии журнала для папки без git: перед каждой записью и перед возвратом из копии."""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from ledger.datadir import DataDir
from ledger.journal import LedgerError, atomic_writer, load, locked, require_writable

KEEP = 30
_FORMAT = "journal-%Y%m%d-%H%M%S-%f.csv"
_NAME = re.compile(r"journal-\d{8}-\d{6}-\d{6}\.csv")
_STEP = timedelta(microseconds=1)


GIT_TIMEOUT = 30  # с на git ls-files


def in_git(root: Path) -> bool:
    """Хранит ли git журнал этой папки: папка в рабочем дереве git, и journal.csv в нём
    отслеживается. Репозиторий выше по дереву, который журнал не хранит (например,
    dotfiles в домашней папке), не считается. Нет git или он не ответил — False:
    тогда копии делаются, это безопасная сторона."""
    if not any((folder / ".git").exists() for folder in (root, *root.parents)):
        return False  # без .git нигде выше git не спрашиваем: так быстрее
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
    }
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "journal.csv"],
            capture_output=True,
            env=env,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _now() -> datetime:
    return datetime.now()


def listing(data: DataDir) -> list[Path]:
    """Копии журнала, новые первыми. Имя копии — время её снятия, поэтому порядок по имени."""
    if not data.backups.is_dir():
        return []
    found = [p for p in data.backups.iterdir() if _NAME.fullmatch(p.name) and p.is_file()]
    return sorted(found, key=lambda p: p.name, reverse=True)


def _next_name(existing: list[Path]) -> str:
    """Имя по текущему времени, но всегда позже самой новой копии: две записи в одну
    микросекунду или часы, ушедшие назад, не дают одинаковых или перепутанных имён."""
    moment = _now()
    for path in existing:
        try:
            newest = datetime.strptime(path.name, _FORMAT)
        except ValueError:  # имя по шаблону, но не дата (например, 13-й месяц): пропустить
            continue
        moment = max(moment, newest + _STEP)
        break
    return moment.strftime(_FORMAT)


def _read(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as fh:
        return fh.read()


def _copy(data: DataDir) -> Path | None:
    """Скопировать журнал в backups/ и оставить KEEP самых новых копий. Нет журнала — None.

    Журнал, равный самой новой копии, второй раз не копируется: пробный импорт или отказ
    в записи не вытесняют настоящую историю. Тогда возвращается эта копия.
    """
    if not data.journal.exists():
        return None
    text = _read(data.journal)
    existing = listing(data)
    if existing and _read(existing[0]) == text:
        return existing[0]
    target = data.backups / _next_name(existing)
    with atomic_writer(target) as fh:
        fh.write(text)
    for old in listing(data)[KEEP:]:
        old.unlink(missing_ok=True)
    return target


def before_write(data: DataDir) -> Path | None:
    """Копия журнала перед записью; в папке под git её не делают: историю хранит git.

    Вызывается под замком журнала (ops._exclusive).
    """
    if in_git(data.root):
        return None
    return _copy(data)


def restore(data: DataDir, name: str) -> Path | None:
    """Вернуть журнал из копии `name` (имя из listing). Возвращает копию журнала,
    каким он был до возврата; None, если журнала не было.

    Копия сначала проверяется по настройкам: с ошибкой журнал не меняется.
    """
    require_writable(data.settings)
    with locked(data.journal):
        chosen = next((p for p in listing(data) if p.name == name), None)
        if chosen is None:
            raise LedgerError(f"копии {name!r} нет в backups/; список показывает restore без имени")
        try:
            load(chosen, data.settings)
        except LedgerError as exc:
            raise LedgerError(
                f"копия {name} не прошла проверку, журнал не изменён: {exc}"
            ) from None
        # Текст копии читается до новой копии: та может вытеснить выбранную, если она самая старая.
        text = _read(chosen)
        current = _copy(data)
        with atomic_writer(data.journal) as fh:
            fh.write(text)
        return current
