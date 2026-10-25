"""Оценка налога книги: облагаемая база, ставка и список того, что решает человек."""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from ledger.calc import ZERO, book_of, my_amount
from ledger.journal import Entry, LedgerError

if TYPE_CHECKING:
    from ledger.settings import Settings, TaxRule

LOSS = (
    "{currency}: база отрицательная (убыток), налог 0 — "
    "проверьте, нет ли минимального налога по правилам режима"
)
NOTE = "оценка, сверить с правилами режима или с бухгалтером"

_MONTH = re.compile(r"(\d{4})-(\d{2})")
_QUARTER = re.compile(r"(\d{4})-Q([1-4])")
_HALF = re.compile(r"(\d{4})-H([12])")
_YEAR = re.compile(r"\d{4}")
_HINTS = {
    "месяц": "налог считается по месяцу: укажите 2026-10",
    "квартал": "налог считается по кварталу: укажите 2026-Q1, 2026-Q2, 2026-Q3 или 2026-Q4",
    "полугодие": "налог считается по полугодию: укажите 2026-H1 или 2026-H2",
    "год": "налог считается по году: укажите 2026",
}


@dataclass(frozen=True)
class TaxResult:
    """needs_decision: (номер записи, причина); строки без записи (убыток по валюте)
    идут после записей с номером 0."""

    base: dict[str, Decimal]
    estimate: dict[str, Decimal]
    needs_decision: list[tuple[int, str]]
    note: str


# Правило налога книги от расширения (задача 9): (записи книги за период, настройки) -> результат.
RuleFn = Callable[[list[Entry], "Settings"], TaxResult]


ANY_PERIOD = "период: 2026-10, 2026-Q3, 2026-H2 или 2026"


def _bounds(text: str, period: str | None) -> tuple[date, date] | None:
    if period in ("месяц", None) and (m := _MONTH.fullmatch(text)) and 1 <= int(m[2]) <= 12:
        year, month = int(m[1]), int(m[2])
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    if period in ("квартал", None) and (m := _QUARTER.fullmatch(text)):
        year, first = int(m[1]), int(m[2]) * 3 - 2
        return date(year, first, 1), date(year, first + 2, calendar.monthrange(year, first + 2)[1])
    if period in ("полугодие", None) and (m := _HALF.fullmatch(text)):
        year, first = int(m[1]), 1 if m[2] == "1" else 7
        return date(year, first, 1), date(year, first + 5, calendar.monthrange(year, first + 5)[1])
    if period in ("год", None) and _YEAR.fullmatch(text):
        return date(int(text), 1, 1), date(int(text), 12, 31)
    return None


def parse_period(text: str, period: str | None) -> tuple[date, date]:
    """«2026-H2» при period «полугодие» → (2026-07-01, 2026-12-31), обе границы включены.

    period None (налог считает расширение, [tax.<книга>] нет): подходит любой вид периода.
    """
    hint = ANY_PERIOD if period is None else _HINTS[period]
    try:
        bounds = _bounds(text.strip().upper(), period)
    except ValueError:  # год 0000
        bounds = None
    if bounds is None:
        raise LedgerError(hint)
    return bounds


def _is_taxable_income(e: Entry, rule: TaxRule) -> bool:
    return e.tax == "да" or (e.category in rule.categories and e.tax != "нет")


def _expense_decision(e: Entry, rule: TaxRule) -> str | None:
    """Расход книги, отметка которого идёт против правила, и возврат, который база не видит."""
    if rule.base == "доходы":
        return "tax=да, но при базе «доходы» расходы не вычитаются" if e.tax == "да" else None
    if e.tax == "нет":
        return "tax=нет, расход не вычитается из базы"
    if e.amount < 0:
        # §6: возвраты в базу не входят, поэтому расходы за период остаются завышенными.
        return "возврат не вычитается из расходов: оценка может быть занижена"
    return None


def _needs_decision(e: Entry, s: Settings, book: str, rule: TaxRule | None) -> str | None:
    """Причина, по которой запись решает человек. Без [tax.<книга>] (rule None) категорий нет,
    и остаётся только доход от покупателя на счёте другой книги."""
    if e.type == "expense":
        if rule is None or book_of(e, s) != book:
            return None
        return _expense_decision(e, rule)
    if e.type != "income":
        return None
    other = book_of(e, s)
    person = s.people.get(e.party)
    if other != book and person and person.role == "покупатель":
        return f"доход от покупателя на счёте книги «{other}»"
    if other != book or rule is None:
        return None
    listed = e.category in rule.categories
    if e.tax == "да" and not listed:
        return "tax=да, но категория не облагается"
    if e.tax == "нет" and listed:
        return "tax=нет, но категория облагается"
    return None


def compute(
    entries: list[Entry],
    s: Settings,
    book: str,
    start: date,
    end: date,
    rule_fn: RuleFn | None = None,
) -> TaxResult:
    """Налог книги за период [start, end].

    rule_fn — правило от расширения; если оно есть, считает оно, а не [tax.<книга>].
    Что требует решения по правилам основы, добавляется и к его результату: отчёт ничего не прячет.
    """
    period = [e for e in entries if start <= e.date <= end]
    rule = s.tax.get(book)
    if rule_fn is not None:
        result = rule_fn([e for e in period if book_of(e, s) == book], s)
        core = [(e.id, r) for e in period if (r := _needs_decision(e, s, book, rule))]
        return replace(result, needs_decision=_merge_decisions(result.needs_decision, core))
    if rule is None:
        raise LedgerError(f"налог для книги «{book}» не настроен")
    base: dict[str, Decimal] = {}
    needs: list[tuple[int, str]] = []
    for e in period:
        reason = _needs_decision(e, s, book, rule)
        if reason:
            needs.append((e.id, reason))
        if book_of(e, s) != book:
            continue
        if e.type == "income" and _is_taxable_income(e, rule):
            base[e.currency] = base.get(e.currency, ZERO) + e.amount
        elif (
            e.type == "expense"
            and rule.base == "доходы-расходы"
            and e.amount > 0
            and e.tax != "нет"
        ):
            base[e.currency] = base.get(e.currency, ZERO) - my_amount(e)
    ordered = {c: base[c] for c in s.currency_order(base)}
    estimate = {c: max(amount, ZERO) * rule.rate / 100 for c, amount in ordered.items()}
    needs.sort()
    needs += [(0, LOSS.format(currency=c)) for c, amount in ordered.items() if amount < 0]
    return TaxResult(ordered, estimate, needs, NOTE)


def _merge_decisions(
    own: list[tuple[int, str]], core: list[tuple[int, str]]
) -> list[tuple[int, str]]:
    """Строки расширения и основы без повторов: записи по номеру, затем строки без записи."""
    unique = list(dict.fromkeys([*own, *core]))
    return sorted(d for d in unique if d[0]) + [d for d in unique if not d[0]]
