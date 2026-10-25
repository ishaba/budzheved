"""Соответствие «продавец → категория» из data/merchants.csv."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path

from ledger.journal import JournalError, atomic_writer

DEFAULT_CATEGORY = "прочее"
EXACT_PREFIX = "="  # «=сквер» совпадает только с описанием «Сквер» целиком
Rules = list[tuple[str, str]]


def normalise(description: str) -> str:
    return " ".join(description.split()).casefold()


def exact_pattern(description: str) -> str:
    return EXACT_PREFIX + normalise(description)


def load_rules(path: Path, categories: Iterable[str]) -> Rules:
    """Прочитать правила. Отсутствующий файл — пустой список правил.

    categories — категории расхода из настроек папки (встроенные и свои из [categories]).
    """
    known = set(categories)
    if not path.exists():
        return []
    rules: Rules = []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != ["pattern", "category"]:
            raise JournalError(f"{path.name}: неожиданный заголовок")
        for row in reader:
            pattern = (row["pattern"] or "").strip()
            exact = pattern.startswith(EXACT_PREFIX)
            pattern = (EXACT_PREFIX if exact else "") + normalise(
                pattern.removeprefix(EXACT_PREFIX)
            )
            category = (row["category"] or "").strip()
            if pattern in ("", EXACT_PREFIX) or category not in known:
                raise JournalError(
                    f"{path.name}, строка {reader.line_num}: "
                    f"пустой шаблон или неизвестная категория {category!r}"
                )
            rules.append((pattern, category))
    return rules


def categorize(description: str, rules: Rules) -> str:
    """Категория первого правила, чей шаблон входит в описание; иначе «прочее»."""
    text = normalise(description)
    for pattern, category in rules:
        if pattern.startswith(EXACT_PREFIX):
            matched = pattern[len(EXACT_PREFIX) :] == text
        else:
            matched = pattern in text
        if matched:
            return category
    return DEFAULT_CATEGORY


def exact_category(description: str, rules: Rules) -> str | None:
    """Категория точного правила для этого описания, если такое правило есть."""
    wanted = exact_pattern(description)
    for pattern, category in rules:
        if pattern == wanted:
            return category
    return None


def save_rules(path: Path, rules: Rules) -> None:
    """Записать правила целиком через временный файл."""
    with atomic_writer(path) as fh:
        writer = csv.writer(fh)
        writer.writerow(["pattern", "category"])
        writer.writerows(rules)
