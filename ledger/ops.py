"""Операции над журналом. Единственное место, где журнал и правила меняются."""

from __future__ import annotations

import functools
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date
from decimal import Decimal

from ledger import backups, kaspi
from ledger.calc import ZERO, balances_by_currency
from ledger.datadir import DataDir
from ledger.journal import (
    FIELDS,
    IMPORTER_NAME,
    PARTY_TYPES,
    Entry,
    JournalError,
    allocate_ids,
    find_duplicates,
    from_row,
    load,
    locked,
    parse_amount,
    parse_shares,
    peek_next_id,
    require_writable,
    save,
    to_row,
    validate,
)
from ledger.merchants import categorize, exact_pattern, load_rules, normalise, save_rules
from ledger.settings import SETTLEMENTS_OFF, Person, Settings


class NotFound(JournalError):
    """В журнале нет записи с таким номером."""


@dataclass
class AddResult:
    entry: Entry | None  # None, если запись не добавлена из-за вероятного дубля
    duplicates: list[Entry]


@dataclass
class ImportResult:
    statement: kaspi.Statement | None  # None у импорта расширения
    problems: list[str]  # непусто — сверка не сошлась, ничего не записано
    added: list[Entry]
    missing: list[Entry]  # есть в журнале, но нет в этой выписке
    typed: list[tuple[Entry, Entry]]  # (новая запись, похожая ручная)
    saved: bool
    # записи журнала, которые уже покрывают строки импорта
    skipped: list[Entry] = dataclass_field(default_factory=list)


REQUIRED_IMPORT_FIELDS = ("date", "type", "amount", "currency", "source_ref")


READ_ONLY_FIELDS = ("id", "source", "source_ref")  # от них зависит поиск дублей выписки
NOT_EDITABLE = "запись правится через агента"


def _exclusive(operation):
    """Держать замок журнала от чтения до записи, чтобы чат и страница не затирали друг друга.

    Журнал старой схемы не меняется: сначала его переводят командой migrate.
    В папке без git перед операцией журнал копируется в backups/.
    """

    @functools.wraps(operation)
    def wrapper(data: DataDir, *args, **kwargs):
        require_writable(data.settings)
        with locked(data.journal):
            backups.before_write(data)
            return operation(data, *args, **kwargs)

    return wrapper


def _rules(data: DataDir):
    """Правила продавцов, проверенные по категориям расхода из настроек папки."""
    return load_rules(data.merchants, data.settings.expense_categories)


def require_valid(entry: Entry, s: Settings) -> None:
    problems = validate(entry, s)
    if problems:
        raise JournalError("; ".join(problems))


def single_person(s: Settings) -> Person:
    """Единственный человек с расчётами: ему достаётся доля, заданная одним числом."""
    people = s.settlement_people()
    if not people:
        raise JournalError(SETTLEMENTS_OFF)
    if len(people) > 1:
        example = ";".join(f"{p.key}:25" for p in people[:2])
        raise JournalError(
            f"людей с расчётами {len(people)}, одной долей не обойтись; "
            f"укажите, чьи доли: {example}"
        )
    return people[0]


def single_shares(s: Settings, percent: int) -> dict[str, int]:
    """Доля одним числом → доли людей; ноль — всё моё."""
    return {single_person(s).key: percent} if percent else {}


def shares_from_spec(s: Settings, spec: str) -> dict[str, int]:
    """«anya:25;boris:25» или голое число для единственного человека; нулевые доли отброшены."""
    spec = spec.strip()
    if spec.isascii() and spec.isdigit():
        return single_shares(s, int(spec))
    return {key: percent for key, percent in parse_shares(spec).items() if percent}


def default_shares(s: Settings) -> dict[str, int]:
    """Доля по умолчанию из settings.toml; при нескольких людях её не угадать."""
    person = single_person(s)
    return {person.key: person.default_share} if person.default_share else {}


def default_party(s: Settings, type: str) -> str:  # noqa: A002 - как поле записи
    """Человек для расчёта и долга, если он с расчётами один; иначе пусто."""
    people = s.settlement_people()
    return people[0].key if type in PARTY_TYPES and len(people) == 1 else ""


def find(entries: list[Entry], ids: list[int]) -> list[Entry]:
    by_id = {e.id: e for e in entries}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise NotFound("нет записей с номерами: " + ", ".join(map(str, unknown)))
    return [by_id[i] for i in ids]


@_exclusive
def add_entry(
    data: DataDir,
    *,
    date: date,
    type: str = "expense",
    amount: str,
    currency: str | None = None,  # None — валюта владельца
    amount2: str = "",
    currency2: str = "",
    category: str = "",
    description: str = "",
    paid_by: str = "me",
    book: str = "",
    account: str = "",
    account2: str = "",
    party: str = "",
    shares: dict[str, int] | None = None,
    tax: str = "",
    source: str = "text",
    receipt: str = "",
    note: str = "",
    force: bool = False,
) -> AddResult:
    settings = data.settings
    entries = load(data.journal, settings)
    if account in settings.accounts and book == settings.accounts[account].book:
        # Книгу счёта даёт сам счёт (calc.book_of): записанная, она заперла бы журнал,
        # когда счёт переедет в другую книгу.
        book = ""
    if not category and type == "expense":
        category = categorize(description, _rules(data))
    if not category and type == "income":
        category = "доход"
    entry = Entry(
        id=peek_next_id(data.journal, entries),
        date=date,
        type=type,
        book=book,
        account=account,
        amount=parse_amount(amount),
        currency=(settings.currency if currency is None else currency).upper(),
        account2=account2,
        amount2=parse_amount(amount2) if amount2 else None,
        currency2=currency2.upper(),
        category=category,
        description=description,
        paid_by=paid_by,
        party=party,
        shares=dict(shares or {}),
        tax=tax,
        source=source,
        receipt=receipt,
        note=note,
    )
    require_valid(entry, settings)
    duplicates = find_duplicates(entries, entry)
    if duplicates and not force:
        return AddResult(None, duplicates)
    entry.id = allocate_ids(data.journal, entries)
    save(data.journal, entries + [entry])
    return AddResult(entry, duplicates)


@_exclusive
def set_shares(data: DataDir, ids: list[int], shares: dict[str, int]) -> list[Entry]:
    entries = load(data.journal, data.settings)
    targets = find(entries, ids)
    not_expenses = [e.id for e in targets if e.type != "expense"]
    if not_expenses:
        raise JournalError(
            "доли ставятся только расходам; не расходы: " + ", ".join(map(str, not_expenses))
        )
    for e in targets:
        e.shares = dict(shares)
        require_valid(e, data.settings)
    save(data.journal, entries)
    return targets


@_exclusive
def edit_entry(
    data: DataDir,
    entry_id: int,
    changes: dict[str, str],
    editable_types: tuple[str, ...] | None = None,
) -> Entry:
    """Изменить поля записи. editable_types — типы, которые этот вызов вправе править
    (страница правит только свои формы); проверка идёт под замком журнала."""
    entries = load(data.journal, data.settings)
    (target,) = find(entries, [entry_id])
    if editable_types is not None and target.type not in editable_types:
        raise JournalError(NOT_EDITABLE)
    row = to_row(target)
    for field, value in changes.items():
        if field not in FIELDS or field in READ_ONLY_FIELDS:
            raise JournalError(f"нельзя изменить поле {field!r}")
        row[field] = value
    updated = from_row(row)
    require_valid(updated, data.settings)
    entries[entries.index(target)] = updated
    save(data.journal, entries)
    return updated


@_exclusive
def delete_entries(data: DataDir, ids: list[int]) -> list[Entry]:
    entries = load(data.journal, data.settings)
    targets = find(entries, ids)
    removed = {e.id for e in targets}
    # Запомнить наибольший номер: номер удалённой записи новой не выдаётся.
    allocate_ids(data.journal, entries, 0)
    save(data.journal, [e for e in entries if e.id not in removed])
    return targets


@_exclusive
def apply_merchant_category(data: DataDir, description: str, category: str) -> list[Entry]:
    """Поставить категорию всем расходам с таким описанием и запомнить правило."""
    key = normalise(description)
    if not key:
        raise JournalError("у записи нет описания, правило не создать")
    if category not in data.settings.expense_categories:
        raise JournalError(f"неизвестная категория расхода {category!r}")
    entries = load(data.journal, data.settings)
    changed = [
        e
        for e in entries
        if e.type == "expense" and normalise(e.description) == key and e.category != category
    ]
    for e in changed:
        e.category = category
    # Правило точное, поэтому первым местом оно не заслоняет других продавцов.
    pattern = exact_pattern(description)
    rules = [(pattern, category)] + [r for r in _rules(data) if r[0] != pattern]
    # Сначала правила: если их не удалось записать, журнал остаётся прежним.
    save_rules(data.merchants, rules)
    if changed:
        save(data.journal, entries)
    return changed


def import_account(s: Settings, importer: str, given: str = "") -> str:
    """Счёт для строк импорта: указанный, иначе единственный счёт с import = importer.

    Без [accounts] счёт не нужен; при нескольких счетах или ни одном отказ.
    """
    if given or not s.has_accounts:
        return given
    keys = [a.key for a in s.accounts.values() if a.importer == importer]
    if len(keys) == 1:
        return keys[0]
    where = (
        f'import = "{importer}" стоит у счетов {", ".join(keys)}'
        if keys
        else f'ни у одного счёта нет import = "{importer}"'
    )
    raise JournalError(f"укажите --account: {where}")


@_exclusive
def import_statement(
    data: DataDir, text: str, dry_run: bool = False, account: str = ""
) -> ImportResult:
    statement = kaspi.parse_statement(text)
    problems = kaspi.reconcile(statement)
    if problems:
        return ImportResult(statement, problems, [], [], [], False)
    entries = load(data.journal, data.settings)
    added = kaspi.to_entries(
        kaspi.new_rows(statement, entries, account),
        _rules(data),
        peek_next_id(data.journal, entries),
        account,
    )
    for entry in added:
        require_valid(entry, data.settings)
    missing = kaspi.missing_entries(statement, entries, account)
    manual = [e for e in entries if e.source != "kaspi-pdf"]
    typed = [(entry, twin) for entry in added for twin in find_duplicates(manual, entry)]
    saved = bool(added) and not dry_run
    if saved:
        allocate_ids(data.journal, entries, len(added))
        save(data.journal, entries + added)
    return ImportResult(statement, [], added, missing, typed, saved)


def _import_entry(row: object, source: str, account: str, rules) -> Entry:
    """Строка импортёра → запись. id и source ставит основа, пустые поля по умолчанию."""
    if not isinstance(row, Mapping):
        raise JournalError(f"ожидался словарь полей журнала, а не {type(row).__name__}")
    unknown = [key for key in row if key not in FIELDS]
    if unknown:
        raise JournalError("неизвестное поле " + ", ".join(map(repr, unknown)))
    text = {
        key: format(value, "f") if isinstance(value, Decimal) else str(value)
        for key, value in row.items()
        if value is not None
    }
    missing = [key for key in REQUIRED_IMPORT_FIELDS if not text.get(key, "").strip()]
    if missing:
        raise JournalError("нет обязательного поля " + ", ".join(missing))
    full = {key: text.get(key, "") for key in FIELDS}
    full.update(id="0", source=source, currency=full["currency"].upper())
    full["paid_by"] = full["paid_by"] or "me"
    full["account"] = full["account"] or account
    if not full["category"] and full["type"] == "expense":
        full["category"] = categorize(full["description"], rules)
    if not full["category"] and full["type"] == "income":
        full["category"] = "доход"
    return from_row(full)


def _totals_problems(entries: list[Entry], totals: Mapping[str, object]) -> list[str]:
    """Итоги импортёра против суммы движения по счетам у всех его строк, по валютам."""
    expected: dict[str, Decimal] = {}
    for currency, value in totals.items():
        try:
            expected[str(currency).upper()] = parse_amount(str(value))
        except JournalError as exc:
            raise JournalError(f"итог {currency}: {exc}") from None
    actual = balances_by_currency(entries)
    problems = []
    for currency in sorted(set(expected) | {c for c, v in actual.items() if v}):
        got = actual.get(currency, ZERO)
        if currency not in expected:
            problems.append(f"{currency}: по строкам {got}, в итогах нет")
        elif got != expected[currency]:
            problems.append(f"{currency}: по строкам {got}, в итогах {expected[currency]}")
    return problems


@_exclusive
def import_rows(
    data: DataDir,
    source_name: str,
    rows: list[dict[str, str]],
    totals: dict[str, str] | None,
    account: str = "",
    dry_run: bool = False,
) -> ImportResult:
    """Записать строки импортёра расширения: проверка каждой, сверка итогов, пропуск дублей.

    Дубль — строка с теми же source и source_ref на том же счёте, что уже в журнале;
    одинаковые строки считаются по количеству, как в выписке Kaspi.
    """
    if not IMPORTER_NAME.fullmatch(source_name):
        raise JournalError(
            f"имя импортёра {source_name!r}: латиница в нижнем регистре, цифры, _ и -"
        )
    source = f"import:{source_name}"
    settings = data.settings
    entries = load(data.journal, settings)
    rules = _rules(data)
    parsed: list[Entry] = []
    errors: list[str] = []
    for number, row in enumerate(rows, 1):
        try:
            entry = _import_entry(row, source, account, rules)
        except JournalError as exc:
            errors.append(f"строка {number}: {exc}")
            continue
        if problems := validate(entry, settings):
            errors.append(f"строка {number}: " + "; ".join(problems))
        parsed.append(entry)
    if errors:
        raise JournalError("импорт отменён, ничего не записано:\n  " + "\n  ".join(errors))
    if totals is not None:
        if problems := _totals_problems(parsed, totals):
            return ImportResult(None, problems, [], [], [], False)
    have: defaultdict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        if e.source == source:
            have[e.source_ref].append(e)
    added: list[Entry] = []
    skipped: list[Entry] = []
    for entry in parsed:
        pool = have[entry.source_ref]
        # Сначала запись того же счёта, потом запись без счёта (kaspi.same_account).
        match = next((e for e in pool if e.account == entry.account), None) or next(
            (e for e in pool if kaspi.same_account(e, entry.account)), None
        )
        if match is None:
            added.append(entry)
        else:
            pool.remove(match)
            skipped.append(match)
    first = peek_next_id(data.journal, entries)
    for offset, entry in enumerate(added):
        entry.id = first + offset
    manual = [e for e in entries if e.source != source]
    typed = [(entry, twin) for entry in added for twin in find_duplicates(manual, entry)]
    saved = bool(added) and not dry_run
    if saved:
        allocate_ids(data.journal, entries, len(added))
        save(data.journal, entries + added)
    return ImportResult(None, [], added, [], typed, saved, skipped)
