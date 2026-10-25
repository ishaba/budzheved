"""Подсчёты по журналу: остатки, долг с людьми, расходы и доходы по категориям."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import TYPE_CHECKING

from ledger.journal import Entry

if TYPE_CHECKING:
    from ledger.settings import Settings

ZERO = Decimal("0")
Effect = dict[tuple[str, str], Decimal]  # (счёт или человек, валюта) -> изменение
Totals = dict[str, Decimal]


def _share(e: Entry) -> Decimal:
    """Общая доля людей в расходе."""
    return Decimal(sum(e.shares.values())) / 100


def _ordered_categories(standard: tuple[str, ...], present: set[str]) -> list[str]:
    return [c for c in standard if c in present] + sorted(present - set(standard))


def book_of(e: Entry, s: Settings) -> str:
    """Книга записи: своя, иначе книга счёта, иначе книга по умолчанию."""
    if e.book:
        return e.book
    account = s.accounts.get(e.account)
    return account.book if account else s.default_book


def _add(effect: Effect, key: tuple[str, str], delta: Decimal) -> None:
    effect[key] = effect.get(key, ZERO) + delta


def balance_effect(e: Entry) -> Effect:
    """Как запись меняет остатки. Ключ: (счёт, валюта); пустой счёт значит «без счёта»."""
    effect: Effect = {}
    if e.type == "expense":
        if e.paid_by == "me":
            _add(effect, (e.account, e.currency), -e.amount)
    elif e.type in ("income", "opening_balance"):
        _add(effect, (e.account, e.currency), e.amount)
    elif e.type == "exchange":
        _add(effect, (e.account, e.currency), -e.amount)
        _add(effect, (e.account2 or e.account, e.currency2), e.amount2 or ZERO)
    elif e.type in ("transfer", "cash_withdrawal") and e.account != e.account2:
        # Снятие без account2 уводит деньги со счёта в «без счёта» (пустой ключ); снятие
        # без обоих счетов, как в журнале без [accounts], остатков не меняет.
        _add(effect, (e.account, e.currency), -e.amount)
        _add(effect, (e.account2, e.currency), e.amount)
    elif e.type == "settlement":
        _add(effect, (e.account, e.currency), -e.amount if e.paid_by == "me" else e.amount)
    return effect


def debt_effect(e: Entry) -> Effect:
    """Как запись меняет долги. Ключ: (человек, валюта).

    Плюс: человек должен мне, минус: я должен ему.
    """
    effect: Effect = {}
    sign = 1 if e.paid_by == "me" else -1
    if e.type == "expense":
        if e.paid_by == "me":
            for person, percent in e.shares.items():
                _add(effect, (person, e.currency), e.amount * Decimal(percent) / 100)
        else:
            _add(effect, (e.paid_by, e.currency), -e.amount * (1 - _share(e)))
    elif e.type in ("settlement", "debt"):
        _add(effect, (e.party, e.currency), sign * e.amount)
    elif e.type == "debt_conversion":
        _add(effect, (e.party, e.currency), -sign * e.amount)
        _add(effect, (e.party, e.currency2), sign * (e.amount2 or ZERO))
    return effect


def my_amount(e: Entry) -> Decimal:
    """Моя часть расхода или дохода; ноль для остальных типов."""
    if e.type == "expense":
        return e.amount * (1 - _share(e))
    if e.type == "income":
        return e.amount
    return ZERO


def _total(entries: Iterable[Entry], effect: Callable[[Entry], Effect]) -> Effect:
    totals: Effect = {}
    for e in entries:
        for key, delta in effect(e).items():
            _add(totals, key, delta)
    return totals


def balances(entries: Iterable[Entry]) -> Effect:
    return _total(entries, balance_effect)


def debts(entries: Iterable[Entry]) -> Effect:
    return _total(entries, debt_effect)


def balances_by_currency(entries: Iterable[Entry]) -> Totals:
    """Остатки по валютам, счета сложены."""
    totals: Totals = {}
    for (_, currency), delta in balances(entries).items():
        totals[currency] = totals.get(currency, ZERO) + delta
    return totals


Row = tuple[str, str, Decimal]  # (счёт или человек, валюта, сумма)


def _rows(totals: Effect, order: Iterable[str], s: Settings) -> list[Row]:
    """Итоги по ключам в порядке order, незнакомые ключи следом по алфавиту;
    внутри ключа валюты по `s.currency_order`."""
    keys = list(dict.fromkeys(order))
    keys += sorted({key for key, _ in totals} - set(keys))
    return [
        (key, currency, totals[(key, currency)])
        for key in keys
        for currency in s.currency_order(c for k, c in totals if k == key)
    ]


def balance_rows(entries: Iterable[Entry], s: Settings) -> list[Row]:
    """Остатки по счетам в порядке `s.accounts`, «без счёта» (пустой ключ) последним."""
    return _rows(balances(entries), [*s.accounts, ""], s)


def debt_rows(entries: Iterable[Entry], s: Settings) -> list[Row]:
    """Долги по людям в порядке `s.people`. Плюс: человек должен мне, минус: я ему."""
    return _rows(debts(entries), s.people, s)


def opening_pairs(entries: Iterable[Entry]) -> set[tuple[str, str]]:
    """Пары «счёт, валюта», по которым внесён стартовый остаток."""
    return {(e.account, e.currency) for e in entries if e.type == "opening_balance"}


def established_currencies(entries: list[Entry]) -> set[str]:
    """Валюты, остаток которых известен целиком: стартовый остаток внесён у каждого счёта,
    чьи записи меняли эту валюту. Иначе итог валюты — только движение с начала журнала."""
    opened = opening_pairs(entries)
    pairs = set(balances(entries))
    currencies = {currency for _, currency in pairs}
    return {c for c in currencies if all(p in opened for p in pairs if p[1] == c)}


def category_totals(
    entries: Iterable[Entry], s: Settings, year: int, month: int, book: str | None = None
) -> dict[tuple[str, str, str], Decimal]:
    """Мои расходы и доходы за месяц. Ключ: (тип, валюта, категория).

    С book берутся только записи этой книги.
    """
    totals: dict[tuple[str, str, str], Decimal] = {}
    for e in entries:
        if e.type not in ("expense", "income"):
            continue
        if (e.date.year, e.date.month) != (year, month):
            continue
        if book is not None and book_of(e, s) != book:
            continue
        key = (e.type, e.currency, e.category)
        totals[key] = totals.get(key, ZERO) + my_amount(e)
    return totals


CategoryGroup = tuple[str, list[tuple[str, Decimal]], Decimal]


def category_groups(
    entries: Iterable[Entry], s: Settings, year: int, month: int, book: str | None = None
) -> dict[str, list[CategoryGroup]]:
    """Итоги месяца для отчёта и страницы: {тип: [(валюта, [(категория, сумма)], итого)]}.

    Категории идут в порядке настроек, валюты по `s.currency_order`; пустые пропущены.
    """
    totals = category_totals(entries, s, year, month, book)
    result: dict[str, list[CategoryGroup]] = {}
    for kind, standard in (
        ("expense", s.expense_categories),
        ("income", s.income_categories),
    ):
        groups = []
        for currency in s.currency_order(c for k, c, _ in totals if k == kind):
            present = {cat for k, c, cat in totals if (k, c) == (kind, currency)}
            rows = [
                (category, totals[(kind, currency, category)])
                for category in _ordered_categories(standard, present)
            ]
            if rows:
                groups.append((currency, rows, sum((amount for _, amount in rows), ZERO)))
        result[kind] = groups
    return result
