"""Настройки папки данных: чтение и проверка settings.toml."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from ledger.journal import EXPENSE_CATEGORIES, INCOME_CATEGORIES, LedgerError

SCHEMA = 2
ROLES = ("партнёр", "работник", "поставщик", "сервис", "покупатель")
TAX_BASES = ("доходы", "доходы-расходы")
TAX_PERIODS = ("месяц", "квартал", "полугодие", "год")

# Отказы, когда возможность выключена настройками (§5: её флаги и команды скрыты).
SETTLEMENTS_OFF = "взаиморасчёты выключены: в settings.toml нет людей с settlements = true"
ACCOUNTS_OFF = "счета выключены: в settings.toml нет [accounts]"
BOOKS_OFF = "книги выключены: в settings.toml нет [books]"

_CURRENCY = re.compile(r"[A-Z]{3}|USDT")
# Ключ человека стоит в journal.csv (paid_by, party, shares «ключ:процент»); «me» — это я.
_PERSON_KEY = re.compile(r"[a-z][a-z0-9_]*")
_TOP_KEYS = ("schema", "code", "owner", "books", "accounts", "people", "tax", "categories")
_SYNTAX_HINT = (
    'в путях пишите / вместо \\, ключи с кириллицей берите в кавычки, например [tax."бизнес"]'
)


class SettingsError(LedgerError):
    """settings.toml нельзя прочитать или в нём ошибка."""


@dataclass(frozen=True)
class Account:
    key: str
    book: str
    currencies: tuple[str, ...]
    importer: str


@dataclass(frozen=True)
class Person:
    key: str
    name: str
    role: str
    settlements: bool
    default_share: int


@dataclass(frozen=True)
class TaxRule:
    book: str
    rate: Decimal
    base: str
    period: str
    categories: tuple[str, ...]


@dataclass(frozen=True)
class Settings:
    schema: int
    code: str
    currency: str
    country: str
    default_book: str
    books: tuple[str, ...]
    accounts: dict[str, Account]
    people: dict[str, Person]
    tax: dict[str, TaxRule]
    expense_categories: tuple[str, ...]
    income_categories: tuple[str, ...]

    def settlement_people(self) -> list[Person]:
        return [p for p in self.people.values() if p.settlements]

    def currency_order(self, codes: Iterable[str]) -> list[str]:
        """Сначала валюта владельца, остальные по алфавиту."""
        unique = set(codes)
        first = [self.currency] if self.currency in unique else []
        return first + sorted(unique - {self.currency})

    @property
    def has_settlements(self) -> bool:
        return bool(self.settlement_people())

    @property
    def has_accounts(self) -> bool:
        return bool(self.accounts)

    @property
    def has_books(self) -> bool:
        return self.books != ("",)


def is_currency(code: str) -> bool:
    return bool(_CURRENCY.fullmatch(code))


def load_settings(path: Path) -> Settings:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SettingsError(
            f"Нет файла настроек {path}. Создайте его командой init или попросите агента."
        ) from None
    except UnicodeDecodeError:
        raise SettingsError(f"{path.name}: файл должен быть в кодировке UTF-8.") from None
    except OSError as exc:
        raise SettingsError(f"Не читается {path}: {exc.strerror or exc}.") from None
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        line = re.search(r"line (\d+)", str(exc))
        where = f", строка {line.group(1)}" if line else ""
        raise SettingsError(
            f"{path.name}: ошибка синтаксиса TOML{where} ({exc}). {_SYNTAX_HINT}."
        ) from None
    return _build(raw)


def _fail(path: str, message: str) -> SettingsError:
    return SettingsError(
        f"settings.toml, {path}: {message}" if path else f"settings.toml: {message}"
    )


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _table(parent: dict[str, Any], key: str, path: str) -> dict[str, Any]:
    value = parent.get(key, {})
    if not isinstance(value, dict):
        raise _fail(_join(path, key), "ожидается таблица [...]")
    return value


def _known(table: dict[str, Any], allowed: tuple[str, ...], path: str) -> None:
    for key in table:
        if key not in allowed:
            raise _fail(_join(path, key), f"неизвестный ключ; допустимы: {', '.join(allowed)}")


def _required(table: dict[str, Any], key: str, path: str) -> Any:
    if key not in table:
        raise _fail(_join(path, key), "обязательный ключ не задан")
    return table[key]


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(path, "ожидается непустая строка")
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _choice(value: Any, options: tuple[str, ...], path: str) -> str:
    if value not in options:
        raise _fail(path, f"допустимы {' | '.join(options)}, а не {value!r}")
    return value


def _currency(value: Any, path: str) -> str:
    if not isinstance(value, str) or not is_currency(value):
        raise _fail(path, f"валюта {value!r}: нужны три заглавные латинские буквы (KZT) или USDT")
    return value


def _strings(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _fail(path, "ожидается список строк")
    return tuple(_text(item, path) for item in value)


def _merge(standard: tuple[str, ...], extra: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*standard, *extra)))


def _build(raw: dict[str, Any]) -> Settings:
    schema = _required(raw, "schema", "")
    if not _is_int(schema) or schema < 1:
        raise _fail("schema", "ожидается целое число, 1 или больше")
    if schema > SCHEMA:
        raise _fail(
            "schema", f"схема {schema} новее кода (он знает до {SCHEMA}); обновите Бюджевед"
        )
    _known(raw, _TOP_KEYS, "")
    code = _text(_required(raw, "code", ""), "code")

    owner = _table(raw, "owner", "")
    if "owner" not in raw:
        raise _fail("owner", "обязательная таблица [owner] не задана")
    _known(owner, ("currency", "country"), "owner")
    currency = _currency(_required(owner, "currency", "owner"), "owner.currency")
    country = _text(_required(owner, "country", "owner"), "owner.country")

    books, default_book = _books(_table(raw, "books", ""))
    accounts = _accounts(_table(raw, "accounts", ""), books, default_book)
    people = _people(_table(raw, "people", ""))
    tax = _tax(_table(raw, "tax", ""), books)

    cats = _table(raw, "categories", "")
    _known(cats, ("expense", "income"), "categories")
    expense = _strings(cats.get("expense", []), "categories.expense")
    income = _strings(cats.get("income", []), "categories.income")

    return Settings(
        schema=schema,
        code=code,
        currency=currency,
        country=country,
        default_book=default_book,
        books=books,
        accounts=accounts,
        people=people,
        tax=tax,
        expense_categories=_merge(EXPENSE_CATEGORIES, expense),
        income_categories=_merge(INCOME_CATEGORIES, income),
    )


def _books(table: dict[str, Any]) -> tuple[tuple[str, ...], str]:
    _known(table, ("default", "names"), "books")
    if not table:
        return ("",), ""
    names = _strings(_required(table, "names", "books"), "books.names")
    if not names:
        raise _fail("books.names", "нужна хотя бы одна книга")
    if len(set(names)) != len(names):
        raise _fail("books.names", "названия книг не должны повторяться")
    default = _text(table["default"], "books.default") if "default" in table else names[0]
    if default not in names:
        raise _fail("books.default", f"книги {default!r} нет в books.names")
    return names, default


def _accounts(
    table: dict[str, Any], books: tuple[str, ...], default_book: str
) -> dict[str, Account]:
    result: dict[str, Account] = {}
    for key, item in table.items():
        path = f"accounts.{key}"
        if not isinstance(item, dict):
            raise _fail(path, "ожидается таблица [accounts.<ключ>]")
        _known(item, ("book", "currencies", "import"), path)
        book = _text(item["book"], f"{path}.book") if "book" in item else default_book
        if book not in books:
            raise _fail(f"{path}.book", f"книги {book!r} нет в books.names")
        raw_currencies = _required(item, "currencies", path)
        if not isinstance(raw_currencies, list) or not raw_currencies:
            raise _fail(f"{path}.currencies", "ожидается непустой список валют")
        currencies = tuple(_currency(c, f"{path}.currencies") for c in raw_currencies)
        importer = _text(item["import"], f"{path}.import") if "import" in item else ""
        result[key] = Account(key, book, currencies, importer)
    return result


def _people(table: dict[str, Any]) -> dict[str, Person]:
    result: dict[str, Person] = {}
    for key, item in table.items():
        path = f"people.{key}"
        if not _PERSON_KEY.fullmatch(key) or key == "me":
            raise _fail(
                path,
                f"ключ человека {key!r}: латиница в нижнем регистре, цифры и _, не me; "
                "например anya",
            )
        if not isinstance(item, dict):
            raise _fail(path, "ожидается таблица [people.<ключ>]")
        _known(item, ("name", "role", "settlements", "default_share"), path)
        name = _text(_required(item, "name", path), f"{path}.name")
        role = _choice(_required(item, "role", path), ROLES, f"{path}.role")
        settlements = item.get("settlements", False)
        if not isinstance(settlements, bool):
            raise _fail(f"{path}.settlements", "ожидается true или false")
        share = item.get("default_share", 0)
        if not _is_int(share):
            raise _fail(f"{path}.default_share", "ожидается целое число от 0 до 100")
        if not 0 <= share <= 100:
            raise _fail(f"{path}.default_share", f"доля {share} вне диапазона от 0 до 100")
        result[key] = Person(key, name, role, settlements, share)
    return result


def _tax(table: dict[str, Any], books: tuple[str, ...]) -> dict[str, TaxRule]:
    result: dict[str, TaxRule] = {}
    for book, item in table.items():
        path = f"tax.{book}"
        if book not in books:
            raise _fail(path, f"книги {book!r} нет в books.names")
        if not isinstance(item, dict):
            raise _fail(path, "ожидается таблица [tax.<книга>]")
        _known(item, ("rate", "base", "period", "categories"), path)
        rate = _required(item, "rate", path)
        if isinstance(rate, bool) or not isinstance(rate, int | float):
            raise _fail(f"{path}.rate", "ожидается число процентов без знака %, например 3")
        if not 0 <= rate <= 100:
            raise _fail(f"{path}.rate", f"ставка {rate} вне диапазона от 0 до 100 процентов")
        base = _choice(_required(item, "base", path), TAX_BASES, f"{path}.base")
        period = _choice(_required(item, "period", path), TAX_PERIODS, f"{path}.period")
        categories = _strings(item.get("categories", []), f"{path}.categories")
        result[book] = TaxRule(book, Decimal(str(rate)), base, period, categories)
    return result
