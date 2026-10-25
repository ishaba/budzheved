"""Журнал операций: модель записи, чтение, запись и проверка CSV."""

from __future__ import annotations

import contextlib
import csv
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

from ledger.lock import exclusive

if TYPE_CHECKING:
    from ledger.settings import Settings

# Валюты, которые страница и команда add предлагают первыми; в журнале допустима любая.
CURRENCIES = ("USDT", "KZT", "RUB", "USD")
TYPES = (
    "expense",
    "income",
    "exchange",
    "cash_withdrawal",
    "transfer",
    "settlement",
    "debt",
    "debt_conversion",
    "opening_balance",
)
TWO_SIDED = ("exchange", "debt_conversion")
PARTY_TYPES = ("settlement", "debt", "debt_conversion")  # у них человек указан в party
CREDITOR_TYPES = ("debt", "debt_conversion")  # у них paid_by означает кредитора
# Деньги владельца журнала: другой плательщик у них числа бы не изменил, а только запутал.
OWN_TYPES = ("exchange", "transfer", "cash_withdrawal", "opening_balance")
TAX_MARKS = ("", "да", "нет")
EXPENSE_CATEGORIES = (
    "продукты",
    "кафе и доставка",
    "транспорт",
    "жильё",
    "связь и подписки",
    "здоровье",
    "одежда",
    "развлечения",
    "подарки",
    "услуги",
    "переводы",
    "прочее",
)
INCOME_CATEGORIES = ("доход", "входящие переводы")
BUILTIN_SOURCES = ("text", "screenshot", "receipt", "kaspi-pdf")
# Имя импортёра расширения: латиница в нижнем регистре, цифры, «_» и «-», без точек и слэшей.
IMPORTER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
_IMPORT_SOURCE = re.compile(r"import:" + IMPORTER_NAME.pattern)
_PERCENT = re.compile(r"[0-9]+")
MIGRATE_HINT = "журнал схемы 1: выполните budget.py migrate --person <ключ>"
MAX_AMOUNT = Decimal("1e15")
MAX_DECIMALS = 8  # знаков после запятой: хватает для USDT и не даёт раздуть файл
MAX_TEXT = 500  # символов в описании и примечании
MAX_SOURCE_REF = 2000
_AMOUNT = re.compile(r"[+-]?[0-9]+(\.[0-9]+)?")
FIELDS = (
    "id",
    "date",
    "type",
    "book",
    "account",
    "amount",
    "currency",
    "account2",
    "amount2",
    "currency2",
    "category",
    "description",
    "paid_by",
    "party",
    "shares",
    "tax",
    "source",
    "source_ref",
    "receipt",
    "note",
)
FIELDS_V1 = (
    "id",
    "date",
    "type",
    "amount",
    "currency",
    "amount2",
    "currency2",
    "category",
    "description",
    "paid_by",
    "person_share",  # доля человека; в журнале схемы 1 на этом месте любое <имя>_share
    "source",
    "source_ref",
    "note",
)


_V1_SHARE_INDEX = FIELDS_V1.index("person_share")
_V1_SHARE = re.compile(r"[a-z][a-z0-9]*_share")


def v1_share_column(fieldnames) -> str | None:
    """Имя колонки доли, если заголовок схемы 1, иначе None.

    Заголовок схемы 1 — FIELDS_V1, где колонка доли называется <имя>_share латиницей.
    """
    names = list(fieldnames or ())
    if len(names) != len(FIELDS_V1):
        return None
    column = names[_V1_SHARE_INDEX]
    others = names[:_V1_SHARE_INDEX] + names[_V1_SHARE_INDEX + 1 :]
    expected = list(FIELDS_V1[:_V1_SHARE_INDEX] + FIELDS_V1[_V1_SHARE_INDEX + 1 :])
    if others != expected or not _V1_SHARE.fullmatch(column):
        return None
    return column


class LedgerError(Exception):
    """Ожидаемый отказ: его текст показывают пользователю как есть."""


class JournalError(LedgerError):
    """Журнал или введённые данные не прошли проверку."""


def parse_month(text: str) -> tuple[int, int]:
    try:
        year, month = text.split("-")
        return date(int(year), int(month), 1).year, int(month)
    except ValueError:
        raise JournalError(f"нужен месяц в виде ГГГГ-ММ, а не {text!r}") from None


@dataclass
class Entry:
    id: int
    date: date
    type: str
    amount: Decimal
    currency: str
    book: str = ""
    account: str = ""
    account2: str = ""
    amount2: Decimal | None = None
    currency2: str = ""
    category: str = ""
    description: str = ""
    paid_by: str = "me"
    party: str = ""
    shares: dict[str, int] = field(default_factory=dict)
    tax: str = ""
    source: str = "text"
    source_ref: str = ""
    receipt: str = ""
    note: str = ""


def parse_amount(text: str) -> Decimal:
    """Разобрать сумму: допускает пробелы в разрядах и запятую как десятичный знак."""
    cleaned = text.strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not _AMOUNT.fullmatch(cleaned):
        raise JournalError(f"не число: {text[:40]!r}")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise JournalError(f"не число: {text[:40]!r}") from None


def parse_shares(text: str) -> dict[str, int]:
    """«anya:25;boris:25» → {"anya": 25, "boris": 25}; пустая строка — всё моё."""
    shares: dict[str, int] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        key, colon, percent = (piece.strip() for piece in part.partition(":"))
        if not colon or not key or not _PERCENT.fullmatch(percent):
            raise JournalError(f"доля {part!r}: нужно ключ:процент, например anya:50")
        if key in shares:
            raise JournalError(f"доля {key!r} указана дважды")
        shares[key] = int(percent)
    total = sum(shares.values())
    if total > 100:
        raise JournalError(f"сумма долей {total} больше 100")
    return shares


def format_shares(shares: dict[str, int]) -> str:
    return ";".join(f"{key}:{percent}" for key, percent in shares.items())


def require_writable(s: Settings) -> None:
    """Записывать можно только журнал той схемы, которую знает код."""
    from ledger.settings import SCHEMA  # settings сам импортирует journal

    if s.schema < SCHEMA:
        raise JournalError(MIGRATE_HINT)


def _is_source(source: str) -> bool:
    return source in BUILTIN_SOURCES or bool(_IMPORT_SOURCE.fullmatch(source))


def _unsafe_receipt(path: str) -> bool:
    """Путь к чеку должен остаться внутри receipts/: не абсолютный и без «..»."""
    windows = PureWindowsPath(path)  # понимает и «/», и «\\»
    return bool(windows.drive or windows.root) or ".." in windows.parts


def _person_problem(key: str, s: Settings, settles: bool = True) -> str | None:
    person = s.people.get(key)
    if person is None:
        return f"человек {key!r} не найден в settings.toml"
    if settles and not person.settlements:
        return f"у человека {key!r} нет взаиморасчётов (settlements = false)"
    return None


def _people_problems(e: Entry, s: Settings) -> list[str]:
    problems = []
    if e.shares:
        if e.type != "expense":
            problems.append("доли людей допустимы только у расхода")
        for key, percent in e.shares.items():
            if problem := _person_problem(key, s):
                problems.append(problem)
            if not isinstance(percent, int) or isinstance(percent, bool) or not 0 <= percent <= 100:
                problems.append(f"доля {key!r}: {percent!r} вне диапазона от 0 до 100")
        total = sum(p for p in e.shares.values() if isinstance(p, int))
        if total > 100:
            problems.append(f"сумма долей {total} больше 100")
    if e.paid_by != "me" and e.type in OWN_TYPES:
        problems.append(f"paid_by {e.paid_by!r}: у {e.type} это всегда me")
    elif e.paid_by != "me":
        if problem := _person_problem(e.paid_by, s):
            problems.append(problem)
        others = [key for key in e.shares if key != e.paid_by]
        if others:
            problems.append(f"платил {e.paid_by!r}, а доли есть у других: {', '.join(others)}")
    if e.party and e.party not in s.people:
        problems.append(f"человек {e.party!r} не найден в settings.toml")
    if e.type in PARTY_TYPES:
        if not e.party:
            problems.append(f"у {e.type} нужен человек в party")
        elif e.party in s.people and (problem := _person_problem(e.party, s)):
            problems.append(problem)
        if e.party and e.paid_by not in ("me", e.party):
            problems.append(f"paid_by {e.paid_by!r}: у {e.type} это me или {e.party!r}")
    return problems


def _account_problems(e: Entry, s: Settings) -> list[str]:
    problems = []
    for key in (e.account, e.account2):
        if key and key not in s.accounts:
            problems.append(f"счёт {key!r} не найден в settings.toml")
    account = s.accounts.get(e.account)
    second = s.accounts.get(e.account2 or e.account)
    for currency, holder in ((e.currency, account), (e.currency2, second)):
        if currency and holder and currency not in holder.currencies:
            problems.append(
                f"валюты {currency} нет у счёта {holder.key!r} ({', '.join(holder.currencies)})"
            )
    if e.type == "transfer":
        if not e.account or not e.account2:
            problems.append("у перевода между счетами нужны оба счёта: account и account2")
        elif e.account == e.account2:
            problems.append("счета перевода должны различаться")
        elif second and e.currency not in second.currencies:
            problems.append(
                f"валюты {e.currency} нет у счёта {second.key!r} ({', '.join(second.currencies)})"
            )
    if e.type == "cash_withdrawal" and e.account2:
        if e.account2 == e.account:
            problems.append("счета снятия наличных должны различаться")
        elif second and e.currency not in second.currencies:
            problems.append(
                f"валюты {e.currency} нет у счёта {second.key!r} ({', '.join(second.currencies)})"
            )
    if e.book and e.book not in s.books:
        problems.append(f"книга {e.book!r} не найдена в settings.toml")
    elif e.book and account and account.book != e.book:
        problems.append(
            f"книга {e.book!r} не совпадает с книгой счёта {e.account!r} ({account.book!r})"
        )
    return problems


def validate(e: Entry, s: Settings) -> list[str]:
    """Вернуть список нарушений; пустой список означает, что запись корректна.

    Люди, счета, книги и категории сверяются с настройками папки данных.
    """
    from ledger.settings import is_currency  # settings сам импортирует journal

    problems = []
    if e.type not in TYPES:
        problems.append(f"неизвестный тип {e.type!r}")
    if not is_currency(e.currency):
        problems.append(f"неизвестная валюта {e.currency!r}")
    if not _is_source(e.source):
        problems.append(f"неизвестный источник {e.source!r}")

    amounts = [v for v in (e.amount, e.amount2) if v is not None]
    if any(abs(v) >= MAX_AMOUNT for v in amounts):
        problems.append("сумма слишком велика")
    if any(v.as_tuple().exponent < -MAX_DECIMALS for v in amounts):
        problems.append(f"в сумме больше {MAX_DECIMALS} знаков после запятой")
    for name, value, limit in (
        ("описание", e.description, MAX_TEXT),
        ("примечание", e.note, MAX_TEXT),
        ("исходная строка выписки", e.source_ref, MAX_SOURCE_REF),
        ("путь к чеку", e.receipt, MAX_TEXT),
    ):
        if len(value) > limit:
            problems.append(f"{name} длиннее {limit} символов")
        if "\n" in value or "\r" in value:
            problems.append(f"{name}: перенос строки недопустим")

    problems += _people_problems(e, s)
    problems += _account_problems(e, s)
    if e.tax not in TAX_MARKS:
        problems.append(f"налог: да, нет или пусто, а не {e.tax!r}")
    elif e.tax and e.type not in ("income", "expense"):
        problems.append("отметка налога допустима только у дохода и расхода")
    if e.receipt and _unsafe_receipt(e.receipt):
        problems.append(
            f"путь к чеку {e.receipt!r} должен вести внутрь receipts/: без .. и не от корня"
        )

    if e.type == "expense":
        if e.amount == 0:
            problems.append("сумма расхода не может быть нулевой")
        if e.category not in s.expense_categories:
            problems.append(f"неизвестная категория расхода {e.category!r}")
    elif e.amount <= 0:
        problems.append("сумма должна быть положительной")

    if e.type == "income":
        if e.category not in s.income_categories:
            problems.append(f"неизвестная категория дохода {e.category!r}")
        if e.paid_by != "me":
            problems.append("доход всегда записывается на меня")
    elif e.type != "expense" and e.category:
        problems.append("категория допустима только у расхода и дохода")

    if e.type in TWO_SIDED:
        if e.amount2 is None or e.amount2 <= 0:
            problems.append("нужна положительная вторая сумма")
        if not is_currency(e.currency2):
            problems.append(f"неизвестная вторая валюта {e.currency2!r}")
        elif e.currency2 == e.currency:
            problems.append("валюты двух сторон должны различаться")
    elif e.amount2 is not None or e.currency2:
        problems.append("вторая сумма допустима только у обмена и пересчёта долга")
    return list(dict.fromkeys(problems))


def from_row(row: dict[str, str]) -> Entry:
    """Собрать запись из строки CSV или из словаря «поле → текст»."""
    try:
        return Entry(
            id=int(row["id"]),
            date=date.fromisoformat(row["date"]),
            type=row["type"],
            book=row["book"],
            account=row["account"],
            amount=parse_amount(row["amount"]),
            currency=row["currency"],
            account2=row["account2"],
            amount2=parse_amount(row["amount2"]) if row["amount2"] else None,
            currency2=row["currency2"],
            category=row["category"],
            description=row["description"],
            paid_by=row["paid_by"],
            party=row["party"],
            shares=parse_shares(row["shares"]),
            tax=row["tax"],
            source=row["source"],
            source_ref=row["source_ref"],
            receipt=row["receipt"],
            note=row["note"],
        )
    except (ValueError, TypeError, AttributeError) as exc:
        raise JournalError(f"не удалось прочитать запись: {exc}") from None


def to_row(e: Entry) -> dict[str, str]:
    return {
        "id": str(e.id),
        "date": e.date.isoformat(),
        "type": e.type,
        "book": e.book,
        "account": e.account,
        "amount": format(e.amount, "f"),
        "currency": e.currency,
        "account2": e.account2,
        "amount2": "" if e.amount2 is None else format(e.amount2, "f"),
        "currency2": e.currency2,
        "category": e.category,
        "description": e.description,
        "paid_by": e.paid_by,
        "party": e.party,
        "shares": format_shares(e.shares),
        "tax": e.tax,
        "source": e.source,
        "source_ref": e.source_ref,
        "receipt": e.receipt,
        "note": e.note,
    }


def load(path: Path, s: Settings) -> list[Entry]:
    """Прочитать и проверить журнал по настройкам папки данных.

    Отсутствующий или пустой файл — пустой журнал.
    """
    if not path.exists() or path.stat().st_size == 0:
        return []
    entries: list[Entry] = []
    seen: set[int] = set()
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if v1_share_column(reader.fieldnames):
            raise JournalError(MIGRATE_HINT)
        if reader.fieldnames != list(FIELDS):
            raise JournalError(f"{path.name}: неожиданный заголовок")
        for row in _rows(reader, path):
            if None in row or None in row.values():
                raise JournalError(f"строка {reader.line_num}: неверное число колонок")
            try:
                entry = from_row(row)
            except JournalError as exc:
                raise JournalError(f"{_where(reader.line_num, row)}: {exc}") from None
            problems = validate(entry, s)
            if entry.id in seen:
                problems.append(f"повтор id {entry.id}")
            if problems:
                raise JournalError(f"{_where(reader.line_num, row)}: " + "; ".join(problems))
            seen.add(entry.id)
            entries.append(entry)
    return entries


def _where(line: int, row: dict[str, str]) -> str:
    """«строка N (запись #ID)»; номер записи, если он читается."""
    try:
        return f"строка {line} (запись #{int(row['id'])})"
    except (ValueError, TypeError, KeyError):
        return f"строка {line}"


def _rows(reader: csv.DictReader, path: Path):
    """Строки CSV; ошибку разбора (например, слишком длинное поле) отдаём как JournalError."""
    try:
        yield from reader
    except csv.Error as exc:
        raise JournalError(f"{path.name}, строка {reader.line_num}: {exc}") from None


REPLACE_TRIES = 10
REPLACE_PAUSE = 0.1  # с; всего ожидания меньше секунды


def replace_retrying(src: str | os.PathLike, dst: str | os.PathLike) -> None:
    """os.replace, который несколько раз повторяет замену занятого файла.

    На Windows антивирус или индексатор на мгновение держат только что записанный файл,
    и замена отказывает с PermissionError. После REPLACE_TRIES попыток ошибка поднимается.
    """
    for attempt in range(1, REPLACE_TRIES + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES:
                raise
            time.sleep(REPLACE_PAUSE)


@contextlib.contextmanager
def atomic_writer(path: Path):
    """Открыть временный файл рядом с path и заменить им path после успешной записи."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as fh:
            yield fh
            fh.flush()
            os.fsync(fh.fileno())
        replace_retrying(name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)
        raise


_held = threading.local()  # замки журналов, которые держит этот поток


@contextlib.contextmanager
def locked(path: Path):
    """Исключительный замок на журнал: один на чат, страницу и любые другие процессы.

    Повторный замок в том же потоке ждал бы сам себя вечно: вместо этого отказ.
    """
    key = str(path.resolve())
    held: set[str] = _held.__dict__.setdefault("paths", set())
    if key in held:
        raise JournalError("журнал уже под замком: запись изнутри другой операции недопустима")
    with exclusive(path.with_name(path.name + ".lock")):
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)


def save(path: Path, entries: list[Entry]) -> None:
    """Записать журнал целиком через временный файл, чтобы сбой не оставил обрывок."""
    with atomic_writer(path) as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for entry in entries:
            writer.writerow(to_row(entry))


def next_id(entries: list[Entry]) -> int:
    return max((e.id for e in entries), default=0) + 1


def _sequence_path(path: Path) -> Path:
    return path.with_suffix(".seq")


def peek_next_id(path: Path, entries: list[Entry]) -> int:
    """Номер, который получит следующая запись. Номера удалённых записей не выдаются
    повторно: наибольший выданный номер хранится рядом с журналом в файле .seq."""
    sequence = _sequence_path(path)
    last = 0
    if sequence.exists():
        try:
            last = int(sequence.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            raise JournalError(f"{sequence.name}: ожидалось число") from None
    return max(last + 1, next_id(entries))


def allocate_ids(path: Path, entries: list[Entry], count: int = 1) -> int:
    """Занять count номеров подряд и вернуть первый."""
    first = peek_next_id(path, entries)
    with atomic_writer(_sequence_path(path)) as fh:
        fh.write(f"{first + count - 1}\n")
    return first


def find_duplicates(entries: list[Entry], candidate: Entry) -> list[Entry]:
    """Записи с той же суммой и валютой и датой в пределах одного дня."""
    return [
        e
        for e in entries
        if e.currency == candidate.currency
        and abs(e.amount) == abs(candidate.amount)
        and abs((e.date - candidate.date).days) <= 1
    ]
