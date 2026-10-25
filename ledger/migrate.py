"""Перевод журнала со схемы 1 на схему 2 со сверкой отчётов до и после.

Журнал схемы 1 вёл расчёты с одним человеком: колонка `<имя>_share` — его доля в расходе,
`paid_by` — `me` или он, `opening_debt` — стартовый долг. Миграция переводит строки в
схему 2, меняет `schema = 1` на `schema = 2` в settings.toml и сверяет цифры отчётов
`balance`, `debt` и `categories` за каждый месяц с формулами схемы 1. Расхождение хотя бы
в одной цифре или любой сбой — откат: журнал и настройки возвращаются байт в байт.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ledger import calc
from ledger.datadir import DataDir
from ledger.journal import (
    FIELDS_V1,
    OWN_TYPES,
    PARTY_TYPES,
    Entry,
    LedgerError,
    atomic_writer,
    from_row,
    load,
    locked,
    replace_retrying,
    save,
    v1_share_column,
)
from ledger.settings import Settings, load_settings

BACKUP_NAME = "journal.v1.csv"
# Миграция ведёт ровно со схемы 1 на схему 2, что бы ни знал код: переход 2 → 3, если он
# появится, будет своим шагом со своими формулами.
TARGET = 2
# Схема 1 у этих типов плательщика не замечала, а схема 2 его не принимает (у дохода это
# отдельная проверка): при переводе он становится me.
PAYER_RESET_TYPES = (*OWN_TYPES, "income")
_V1_AMOUNT = re.compile(r"[+-]?[0-9]+(\.[0-9]+)?")
ZERO = Decimal("0")
V1_SHARE = "share"  # доля человека в сырой строке; в файле колонка называется <имя>_share
# Строка `schema = 1` верхнего уровня; меняется только цифра, комментарий остаётся.
_SCHEMA_LINE = re.compile(r"^([ \t]*schema[ \t]*=[ \t]*)1(?=[ \t]*(?:#[^\r\n]*)?\r?$)", re.M)
_TABLE = re.compile(r"^[ \t]*\[", re.M)
MAX_LISTED = 5  # сколько расхождений назвать в отказе


class MigrationError(LedgerError):
    """Миграция отказала; журнал и настройки остались как были."""


@dataclass
class MigrationReport:
    rows: int
    months: list[str]
    backup: Path
    # записи обмена, снятия, стартового остатка и дохода, у которых paid_by человека стал me
    payer_reset: list[int] = field(default_factory=list)


Row = dict[str, str]
Totals = dict[str, Decimal]


# --- Формулы схемы 1, заморожены построчно с calc.py той версии ------------------------
# Работают по сырым строкам схемы 1 из _v1_rows. Человек — любой paid_by, кроме "me".


def _v1_decimal(text: str) -> Decimal:
    """Сумма так, как её читал parse_amount схемы 1: пробелы в разрядах, десятичная запятая,
    только цифры (без NaN, Infinity и 1e3). Своя копия, чтобы не зависеть от journal."""
    cleaned = text.strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not _V1_AMOUNT.fullmatch(cleaned):
        raise MigrationError(f"не число: {text[:40]!r}")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise MigrationError(f"не число: {text[:40]!r}") from None


def _v1_share(r: Row) -> Decimal:
    return Decimal(int(r[V1_SHARE])) / 100


def _v1_balance_effect(r: Row) -> Totals:
    """Как запись меняет мои остатки, по валютам."""
    amount = _v1_decimal(r["amount"])
    if r["type"] == "expense":
        return {r["currency"]: -amount} if r["paid_by"] == "me" else {}
    if r["type"] in ("income", "opening_balance"):
        return {r["currency"]: amount}
    if r["type"] == "exchange":
        return {r["currency"]: -amount, r["currency2"]: _v1_decimal(r["amount2"])}
    if r["type"] == "settlement":
        return {r["currency"]: -amount if r["paid_by"] == "me" else amount}
    return {}


def _v1_debt_effect(r: Row) -> Totals:
    """Как запись меняет долг. Плюс: человек должен мне, минус: я должен ему."""
    amount = _v1_decimal(r["amount"])
    sign = 1 if r["paid_by"] == "me" else -1
    if r["type"] == "expense":
        if r["paid_by"] == "me":
            return {r["currency"]: amount * _v1_share(r)}
        return {r["currency"]: -amount * (1 - _v1_share(r))}
    if r["type"] in ("settlement", "opening_debt"):
        return {r["currency"]: sign * amount}
    if r["type"] == "debt_conversion":
        return {r["currency"]: -sign * amount, r["currency2"]: sign * _v1_decimal(r["amount2"])}
    return {}


def _v1_my_amount(r: Row) -> Decimal:
    """Моя часть расхода или дохода; ноль для остальных типов."""
    if r["type"] == "expense":
        return _v1_decimal(r["amount"]) * (1 - _v1_share(r))
    if r["type"] == "income":
        return _v1_decimal(r["amount"])
    return ZERO


def _v1_total(rows: Iterable[Row], effect: Callable[[Row], Totals]) -> Totals:
    totals: Totals = {}
    for r in rows:
        for currency, delta in effect(r).items():
            totals[currency] = totals.get(currency, ZERO) + delta
    return totals


def _v1_balances(rows: Iterable[Row]) -> Totals:
    return _v1_total(rows, _v1_balance_effect)


def _v1_debts(rows: Iterable[Row]) -> Totals:
    return _v1_total(rows, _v1_debt_effect)


def _v1_categories(
    rows: Iterable[Row], year: int, month: int
) -> dict[tuple[str, str, str], Decimal]:
    """Мои расходы и доходы за месяц. Ключ: (тип, валюта, категория)."""
    totals: dict[tuple[str, str, str], Decimal] = {}
    for r in rows:
        if r["type"] not in ("expense", "income"):
            continue
        day = date.fromisoformat(r["date"])
        if (day.year, day.month) != (year, month):
            continue
        key = (r["type"], r["currency"], r["category"])
        totals[key] = totals.get(key, ZERO) + _v1_my_amount(r)
    return totals


# --- Перевод строк -------------------------------------------------------------------


def _translate(r: Row, person: str) -> Row:
    """Строка схемы 1 → строка схемы 2; новые поля пусты."""
    kind = "debt" if r["type"] == "opening_debt" else r["type"]
    share = int(r[V1_SHARE])
    return {
        "id": r["id"],
        "date": r["date"],
        "type": kind,
        "book": "",
        "account": "",
        "amount": r["amount"],
        "currency": r["currency"],
        "account2": "",
        "amount2": r["amount2"],
        "currency2": r["currency2"],
        "category": r["category"],
        "description": r["description"],
        # У обмена, снятия, стартового остатка и дохода формулы схемы 1 плательщика не
        # замечали, а схема 2 его там не принимает: деньги всё равно мои.
        "paid_by": "me" if r["paid_by"] == "me" or kind in PAYER_RESET_TYPES else person,
        "party": person if kind in PARTY_TYPES else "",
        "shares": f"{person}:{share}" if share else "",
        "tax": "",
        "source": r["source"],
        "source_ref": r["source_ref"],
        "receipt": "",
        "note": r["note"],
    }


def _entries(rows: list[Row], person: str) -> list[Entry]:
    entries = []
    for r in rows:
        try:
            entries.append(from_row(_translate(r, person)))
        except (LedgerError, ValueError) as exc:
            raise MigrationError(f"запись #{r['id']}: {exc}") from None
    return entries


# --- Файлы ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise MigrationError(f"нет файла {path.name}") from None
    except UnicodeDecodeError:
        raise MigrationError(f"{path.name}: файл должен быть в кодировке UTF-8") from None


def _write_text(path: Path, text: str) -> None:
    """Записать текст байт в байт (UTF-8, переводы строк как есть) с атомарной заменой."""
    with atomic_writer(path) as fh:
        fh.write(text)


def _v1_rows(text: str, name: str) -> list[Row]:
    """Строки журнала схемы 1; колонка доли, как бы она ни называлась, лежит под V1_SHARE."""
    reader = csv.DictReader(io.StringIO(text, newline=""))
    try:
        column = v1_share_column(reader.fieldnames)
        if column is None:
            raise MigrationError(
                f"{name}: неожиданный заголовок; у журнала схемы 1 колонки "
                f"{','.join(FIELDS_V1)}, где вместо person_share любое <имя>_share"
            )
        rows = list(reader)
    except csv.Error as exc:
        raise MigrationError(f"{name}, строка {reader.line_num}: {exc}") from None
    for r in rows:
        if None in r or None in r.values():
            raise MigrationError(f"{name}, запись #{r.get('id')}: неверное число колонок")
        r[V1_SHARE] = r.pop(column)
    if not rows:
        raise MigrationError(f"{name}: в журнале нет записей, переводить нечего")
    return rows


def _bump_schema(text: str) -> str:
    """Поменять в тексте settings.toml ровно строку `schema = 1`, остальное не трогать."""
    table = _TABLE.search(text)
    head = text[: table.start()] if table else text
    found = list(_SCHEMA_LINE.finditer(head))
    if len(found) != 1:
        raise MigrationError("в settings.toml не найдена одна строка schema = 1 до первой таблицы")
    digit = found[0].end(1)
    return text[:digit] + str(TARGET) + text[digit + 1 :]


# --- Проверки --------------------------------------------------------------------------


def _require_person(s: Settings, person: str) -> None:
    found = s.people.get(person)
    if found is None:
        raise MigrationError(f"человек {person!r} не найден в settings.toml")
    if not found.settlements:
        raise MigrationError(
            f"у человека {person!r} нет взаиморасчётов: поставьте settlements = true"
        )


def _require_one_payer(rows: list[Row]) -> None:
    payers = sorted({r["paid_by"] for r in rows} - {"me"})
    if len(payers) > 1:
        raise MigrationError(
            "в журнале схемы 1 платят разные люди: " + ", ".join(payers) + "; ожидался один"
        )


def _label(key: object) -> str:
    return " ".join(key) if isinstance(key, tuple) else str(key)


def _compare(report: str, before: dict, after: dict) -> list[str]:
    """Расхождения цифр; ключа нет с одной стороны — там ноль."""
    problems = []
    for key in sorted(set(before) | set(after)):
        left, right = before.get(key, ZERO), after.get(key, ZERO)
        if left != right:
            problems.append(f"{report} {_label(key)}: {left:f} ≠ {right:f}")
    return problems


def _check(data: DataDir, rows: list[Row], person: str, old: Settings) -> list[str]:
    """Прочитать переведённый журнал с новыми настройками и сверить отчёты; вернуть месяцы."""
    settings = load_settings(data.settings_file)
    if replace(settings, schema=old.schema) != old or settings.schema != TARGET:
        raise MigrationError("settings.toml после смены схемы читается иначе")
    entries = load(data.journal, settings)
    problems = []
    if len(entries) != len(rows):
        problems.append(f"rows: {len(rows)} ≠ {len(entries)}")
    problems += _compare("balance", _v1_balances(rows), calc.balances_by_currency(entries))
    new_debts: Totals = {}
    for (who, currency), amount in calc.debts(entries).items():
        if who == person:
            new_debts[currency] = amount
        elif amount:
            problems.append(f"debt {who} {currency}: 0 ≠ {amount:f}")
    problems += _compare("debt", _v1_debts(rows), new_debts)
    months = sorted({r["date"][:7] for r in rows})
    for month in months:
        year, number = (int(part) for part in month.split("-"))
        problems += _compare(
            f"categories {month}",
            _v1_categories(rows, year, number),
            calc.category_totals(entries, settings, year, number),
        )
    if problems:
        shown = problems[:MAX_LISTED]
        rest = len(problems) - len(shown)
        raise MigrationError(
            "не сошлось: " + "; ".join(shown) + (f"; и ещё {rest}" if rest else "")
        )
    return months


def _rollback(data: DataDir, backup: Path, settings_text: str) -> str | None:
    """Вернуть журнал из копии и прежний settings.toml; копии после этого нет.

    Сбой отката не прячет причину миграции: возвращается подсказка, что сделать руками.
    """
    try:
        replace_retrying(backup, data.journal)
    except OSError as exc:
        return f"откат не удался ({exc}): журнал в {backup.name}, поставьте schema = 1"
    try:
        _write_text(data.settings_file, settings_text)
    except OSError as exc:
        return f"откат settings.toml не удался ({exc}): журнал возвращён, поставьте schema = 1"
    return None


def migrate(data: DataDir, person: str) -> MigrationReport:
    """Перевести журнал папки данных со схемы 1 на схему 2; `person` — ключ человека,
    с которым велись расчёты в схеме 1. Копия прежнего журнала остаётся в journal.v1.csv."""
    backup = data.journal.with_name(BACKUP_NAME)
    with locked(data.journal):
        settings_text = _read_text(data.settings_file)
        old = load_settings(data.settings_file)  # не data.settings: та могла быть прочитана раньше
        if old.schema >= TARGET:
            raise MigrationError(f"уже схема {old.schema}: журнал переводить не нужно")
        new_settings_text = _bump_schema(settings_text)
        _require_person(old, person)
        if backup.exists():
            raise MigrationError(
                f"{backup.name} уже есть: прошлая миграция не убрана; "
                "проверьте журнал и уберите копию, потом повторите"
            )
        original = _read_text(data.journal)
        rows = _v1_rows(original, data.journal.name)
        _require_one_payer(rows)
        entries = _entries(rows, person)

        _write_text(backup, original)
        try:
            save(data.journal, entries)
            _write_text(data.settings_file, new_settings_text)
            months = _check(data, rows, person, old)
        except BaseException as exc:
            failed = _rollback(data, backup, settings_text)
            reason = str(exc) if isinstance(exc, LedgerError) else f"{type(exc).__name__}: {exc}"
            if failed:
                raise MigrationError(f"миграция отменена: {reason}; {failed}") from exc
            if isinstance(exc, LedgerError):
                raise MigrationError(
                    f"миграция отменена, журнал и настройки возвращены: {reason}"
                ) from None
            raise
    reset = [int(r["id"]) for r in rows if r["type"] in PAYER_RESET_TYPES and r["paid_by"] != "me"]
    return MigrationReport(rows=len(rows), months=months, backup=backup, payer_reset=reset)
