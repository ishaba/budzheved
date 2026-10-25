"""Данные для страницы: всё, что она показывает, посчитано и отформатировано здесь."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from ledger.calc import (
    balances_by_currency,
    category_groups,
    debt_rows,
    established_currencies,
)
from ledger.journal import CURRENCIES, PARTY_TYPES, TYPES, Entry, to_row
from ledger.reports import (
    TYPE_LABELS,
    debt_line,
    entry_line,
    format_money,
    format_signed,
    person_name,
)

if TYPE_CHECKING:
    from ledger.settings import Settings

Month = tuple[int, int]
SHARE_PRESETS = (0, 50, 100)  # переключатель доли единственного человека с расчётами
# Типы, у которых есть форма на странице; перевод между счетами правится через агента.
PAGE_TYPES = tuple(t for t in TYPES if t != "transfer")  # счетов на странице пока нет


def entry_dict(e: Entry, s: Settings) -> dict:
    """Запись для страницы: поля журнала строками (shares как «anya:50»), плюс подписи."""
    data: dict = to_row(e)
    data.update(
        id=e.id,
        share=sum(e.shares.values()),
        type_label=TYPE_LABELS[e.type],
        editable=e.type in PAGE_TYPES,
        amount_text=format_money(e.amount),
        amount2_text="" if e.amount2 is None else format_money(e.amount2),
        line=entry_line(e, s),
    )
    return data


def month_key(month: Month) -> str:
    return f"{month[0]:04d}-{month[1]:02d}"


def _default_month(entries: list[Entry]) -> Month:
    latest = max((e.date for e in entries), default=date.today())
    return latest.year, latest.month


def _balances(entries: list[Entry], s: Settings) -> list[dict]:
    totals = balances_by_currency(entries)
    established = established_currencies(entries)
    tiles = []
    for currency in s.currency_order(totals):
        amount = totals.get(currency, Decimal("0"))
        has_opening = currency in established
        tiles.append(
            {
                "currency": currency,
                "has_opening": has_opening,
                "text": format_money(amount) if has_opening else format_signed(amount),
            }
        )
    return tiles


def _debts(entries: list[Entry], s: Settings) -> list[dict]:
    """Долги по паре «человек, валюта», в том же порядке, что в report debt."""
    rows = []
    for person, currency, amount in debt_rows(entries, s):
        if format_money(amount) == "0,00":
            continue
        rows.append(
            {
                "person": person,
                "name": person_name(s, person),
                "currency": currency,
                "direction": "they_owe" if amount > 0 else "i_owe",
                "text": format_money(abs(amount)),
                "line": debt_line(person_name(s, person), currency, amount),
            }
        )
    return rows


def _share_person(s: Settings) -> dict | None:
    """Человек, которому достаётся доля с переключателя: только если он с расчётами один."""
    people = s.settlement_people()
    if len(people) != 1:
        return None
    return {"key": people[0].key, "name": people[0].name}


def _types(s: Settings) -> list[dict]:
    """Расчёт и долг страница пишет только единственному человеку: выбора человека на ней нет."""
    one = _share_person(s) is not None
    return [
        {"value": t, "label": TYPE_LABELS[t]} for t in PAGE_TYPES if one or t not in PARTY_TYPES
    ]


def _currencies(s: Settings) -> list[str]:
    """Валюты для формы: валюта владельца первой, затем привычные и валюты счетов по алфавиту."""
    held = (c for account in s.accounts.values() for c in account.currencies)
    return s.currency_order({s.currency, *CURRENCIES, *held})


def _categories(entries: list[Entry], s: Settings, month: Month) -> dict:
    return {
        kind: [
            {
                "currency": currency,
                "total_text": format_money(total),
                "rows": [
                    {"category": category, "text": format_money(amount)}
                    for category, amount in rows
                ],
            }
            for currency, rows, total in groups
        ]
        for kind, groups in category_groups(entries, s, *month).items()
    }


def state(entries: list[Entry], s: Settings, month: Month | None = None) -> dict:
    month = month or _default_month(entries)
    months = sorted({month_key((e.date.year, e.date.month)) for e in entries}, reverse=True)
    ordered = sorted(entries, key=lambda e: (e.date, e.id), reverse=True)
    first = min((e.date for e in entries), default=date.today())
    return {
        "entries": [entry_dict(e, s) for e in ordered],
        "month": month_key(month),
        "months": months,
        "first_date": first.isoformat(),
        "today": date.today().isoformat(),
        "balances": _balances(entries, s),
        "debts": _debts(entries, s),
        "share_person": _share_person(s),
        "categories": _categories(entries, s, month),
        "options": {
            "currencies": _currencies(s),
            "types": _types(s),
            "expense_categories": list(s.expense_categories),
            "income_categories": list(s.income_categories),
            "payers": [
                {"value": "me", "label": "я"},
                *({"value": p.key, "label": p.name} for p in s.settlement_people()),
            ],
            "shares": list(SHARE_PRESETS),
        },
    }
