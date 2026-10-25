"""Выгрузка журнала в CSV, который Excel в русской локали открывает таблицей."""

from __future__ import annotations

import csv
import os
from collections.abc import Iterable
from pathlib import Path

from ledger.datadir import DataDir
from ledger.journal import FIELDS, Entry, LedgerError, atomic_writer, to_row
from ledger.settings import Settings

AMOUNT_FIELDS = ("amount", "amount2")
BOM = "\ufeff"  # по нему Excel узнаёт UTF-8
# С этих знаков Excel начинает формулу: такой текст получает впереди «'» и остаётся текстом.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


DATA_FILES = (
    "journal.csv",
    "journal.seq",
    "journal.csv.lock",
    "journal.v1.csv",
    "settings.toml",
    "merchants.csv",
    "AGENTS.md",
    "budget.py",
)
DATA_FOLDERS = ("backups", "extensions")
REFUSED = "выгрузка не пишется поверх файлов папки данных: {path}; выберите другое имя"


def _same(a: Path, b: Path) -> bool:
    """Один и тот же файл: по пути после ссылок, а если оба есть — по самому файлу
    (жёсткая ссылка, другой регистр букв на Windows и macOS)."""
    if a == b:
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def require_free_target(data: DataDir, path: Path) -> None:
    """Отказать, если выгрузка заменила бы журнал, настройки, правила, копии или расширения."""
    target = path.expanduser().resolve()
    root = data.root.resolve()
    folders = [root / name for name in DATA_FOLDERS]
    inside = any(target.is_relative_to(folder) for folder in folders)
    if inside or any(_same(target, root / name) for name in DATA_FILES):
        raise LedgerError(REFUSED.format(path=path))
    parent = target.parent
    if any(_same(parent, folder) for folder in folders):
        raise LedgerError(REFUSED.format(path=path))


def cell(text: str) -> str:
    """Текстовая ячейка, которую Excel не выполнит как формулу."""
    return "'" + text if text.startswith(FORMULA_START) else text


def excel(entries: Iterable[Entry], s: Settings, path: Path) -> int:
    """Записать записи в порядке журнала: колонки FIELDS, разделитель «;», суммы с
    десятичной запятой, текст, похожий на формулу, с «'» впереди, UTF-8 с BOM.
    Файл заменяется целиком; возвращает число записей.

    Настройки `s` принимаются для единообразия с отчётами и на выгрузку не влияют.
    """
    count = 0
    try:
        with atomic_writer(path) as fh:
            fh.write(BOM)
            writer = csv.DictWriter(fh, fieldnames=FIELDS, delimiter=";")
            writer.writeheader()
            for entry in entries:
                row = {
                    name: value.replace(".", ",") if name in AMOUNT_FIELDS else cell(value)
                    for name, value in to_row(entry).items()
                }
                writer.writerow(row)
                count += 1
    except OSError as exc:
        raise LedgerError(
            f"не удалось записать {path}: {exc.strerror or exc}. "
            "Если файл открыт в Excel, закройте его и повторите"
        ) from None
    return count
