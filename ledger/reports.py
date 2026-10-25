"""Текстовые отчёты и однострочное представление записи."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from ledger.calc import balance_rows, category_groups, debt_effect, debt_rows, opening_pairs
from ledger.journal import CREDITOR_TYPES, PARTY_TYPES, Entry

if TYPE_CHECKING:
    from ledger.settings import Settings
    from ledger.tax import TaxResult

TYPE_LABELS = {
    "expense": "расход",
    "income": "доход",
    "exchange": "обмен",
    "cash_withdrawal": "снятие наличных",
    "transfer": "перевод между счетами",
    "settlement": "расчёт",
    "debt": "долг",
    "debt_conversion": "пересчёт долга",
    "opening_balance": "стартовый остаток",
}


def format_money(value: Decimal) -> str:
    """163992.41 → «163 992,41». Округление до копеек только здесь."""
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = Decimal("0.00")
    return f"{rounded:,.2f}".replace(",", " ").replace(".", ",")


def format_signed(value: Decimal) -> str:
    text = format_money(value)
    return text if text.startswith("-") else "+" + text


def person_name(s: Settings, key: str) -> str:
    person = s.people.get(key)
    return person.name if person else key


NO_ACCOUNT = "без счёта"


def entry_line(e: Entry, s: Settings) -> str:
    parts = [
        f"#{e.id}",
        e.date.isoformat(),
        TYPE_LABELS[e.type],
        f"{format_money(e.amount)} {e.currency}",
    ]
    if e.amount2 is not None:
        parts.append(f"→ {format_money(e.amount2)} {e.currency2}")
    if e.account2:
        parts.append(f"(счета {e.account or NO_ACCOUNT} → {e.account2})")
    elif e.account:
        parts.append(f"(счёт {e.account})")
    if e.category:
        parts.append(f"[{e.category}]")
    if e.description:
        parts.append(e.description)
    if e.type in PARTY_TYPES and e.party:
        parts.append(f"(с {person_name(s, e.party)})")
    if e.type in CREDITOR_TYPES:
        creditor = "я" if e.paid_by == "me" else person_name(s, e.paid_by)
        parts.append(f"(кредитор {creditor})")
    elif e.paid_by != "me":
        parts.append(f"(платил {person_name(s, e.paid_by)})")
    if e.shares:
        shares = ", ".join(f"{person_name(s, k)} {v}%" for k, v in e.shares.items())
        parts.append(f"(доли: {shares})")
    if e.note:
        parts.append(f"({e.note})")
    return " ".join(parts)


def balance_report(entries: list[Entry], s: Settings) -> str:
    """Остатки по валютам; со счетами в настройках — по паре «счёт, валюта»."""
    opened = opening_pairs(entries)
    lines = ["Остатки:"]
    for account, currency, amount in balance_rows(entries, s):
        where = f"{account or NO_ACCOUNT}, {currency}" if s.has_accounts else currency
        if (account, currency) in opened:
            lines.append(f"  {where}: {format_money(amount)}")
        else:
            lines.append(
                f"  {where}: {format_signed(amount)} "
                "(без стартового остатка, изменение с начала журнала)"
            )
    if len(lines) == 1:
        lines.append("  записей нет")
    return "\n".join(lines)


def categories_report(
    entries: list[Entry], s: Settings, year: int, month: int, book: str | None = None
) -> str:
    """Мои расходы и доходы за месяц; с book только по этой книге."""
    groups = category_groups(entries, s, year, month, book)
    suffix = "" if book is None else f", книга {book}"
    lines = []
    for title, kind in (("Расходы", "expense"), ("Доходы", "income")):
        lines.append(f"{title} за {year:04d}-{month:02d}{suffix}:")
        for currency, rows, total in groups[kind]:
            lines.append(f"  {currency}")
            for category, amount in rows:
                lines.append(f"    {category}: {format_money(amount)}")
            lines.append(f"    итого: {format_money(total)}")
        if not groups[kind]:
            lines.append("  нет записей")
    return "\n".join(lines)


def debt_line(name: str, currency: str, amount: Decimal) -> str:
    """«Аня, KZT: долг мне 1 000,00» или «…: мой долг …»; одна строка для отчёта и страницы."""
    if amount > 0:
        return f"{name}, {currency}: долг мне {format_money(amount)}"
    return f"{name}, {currency}: мой долг {format_money(-amount)}"


def debt_report(entries: list[Entry], s: Settings) -> str:
    """Долг по паре «человек, валюта», затем операции, которые его меняли."""
    lines = ["Долг:"]
    for person, currency, amount in debt_rows(entries, s):
        if format_money(amount) == "0,00":
            continue
        lines.append(f"  {debt_line(person_name(s, person), currency, amount)}")
    if len(lines) == 1:
        lines.append("  мы в расчёте")
    named = len(s.settlement_people()) > 1  # с одним человеком его имя в операциях лишнее
    operations = []
    for e in entries:
        effect = [(person, c, v) for (person, c), v in debt_effect(e).items() if v]
        if not effect:
            continue
        change = ", ".join(
            (f"{person_name(s, person)} " if named else "") + f"{format_signed(v)} {c}"
            for person, c, v in effect
        )
        operations.append(f"  {entry_line(e, s)} | долг {change}")
    if operations:
        lines.append("Операции (плюс: долг мне, минус: мой долг):")
        lines.extend(operations)
    return "\n".join(lines)


def tax_report(result: TaxResult, book: str, period_text: str) -> str:
    """База и оценка по каждой валюте, затем то, что человек решает сам."""
    lines = [f"Налог, книга «{book}», {period_text} ({result.note}):"]
    for currency, base in result.base.items():
        estimate = result.estimate[currency]
        lines.append(f"  {currency}: база {format_money(base)}, налог {format_money(estimate)}")
    if not result.base:
        lines.append("  облагаемых доходов нет")
    if result.needs_decision:
        lines.append("Требует решения:")
        lines.extend(
            f"  #{entry_id} {reason}" if entry_id else f"  {reason}"
            for entry_id, reason in result.needs_decision
        )
    return "\n".join(lines)
