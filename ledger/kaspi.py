"""Импорт выписки Kaspi Gold: разбор текста PDF, сверка с шапкой, поиск дублей."""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from ledger.journal import Entry, LedgerError
from ledger.merchants import Rules, categorize, exact_category

_NUM = r"[\d  ]+,\d{2}"
_DATE = r"\d{2}\.\d{2}\.\d{2}"
_OPERATIONS = "Покупка|Перевод|Пополнение|Снятие|Разное"
_ROW = re.compile(rf"^\s*({_DATE})\s+([+-])\s*({_NUM})\s*₸\s+({_OPERATIONS})\s+(.*\S)\s*$")
_DATED = re.compile(rf"^\s*{_DATE}\s")
_FOREIGN = re.compile(rf"^\s*\([+-]\s*({_NUM})\s*([A-Z]{{3}})\)\s*$")
_SUMMARY = re.compile(
    rf"^\s*(Доступно на {_DATE}|Пополнения|Поступления со своих счетов"
    rf"|Зачисления кредитов|Переводы на свои счета|Переводы|Покупки|Снятия|Разное)"
    rf"\s+([+-])\s*({_NUM})\s*₸"
)
_HEADER = re.compile(r"^\s*Дата\s+Сумма\s+Операция\s+Детали\s*$")
_PERIOD = re.compile(rf"за период с ({_DATE}) по ({_DATE})")
_SKIP = ("АО «Kaspi Bank»", "Приложение к Справке")
_END = ("Сумма заблокирована", "Раздел «Краткое содержание")

GROUPS = {
    "Покупка": "Покупки",
    "Перевод": "Переводы",
    "Пополнение": "Пополнения",
    "Снятие": "Снятия",
    "Разное": "Разное",
}
UNSUPPORTED_GROUPS = (
    "Поступления со своих счетов",
    "Зачисления кредитов",
    "Переводы на свои счета",
)
_FIXED_CATEGORIES = {"Перевод": "переводы", "Разное": "прочее"}


class KaspiError(LedgerError):
    """Выписку не удалось прочитать или разобрать."""


@dataclass(frozen=True)
class KaspiRow:
    date: date
    amount: Decimal  # со знаком, как в выписке
    operation: str
    details: str
    note: str = ""

    @property
    def ref(self) -> str:
        """Ключ строки для поиска дублей; попадает в source_ref журнала."""
        return f"{self.date.isoformat()}|{self.amount}|{self.operation}|{self.details}"


@dataclass
class Statement:
    period_from: date
    period_to: date
    opening: Decimal
    closing: Decimal
    summary: dict[str, Decimal]
    rows: list[KaspiRow]  # в порядке выписки: от новых к старым


def _date(text: str) -> date:
    day, month, year = text.split(".")
    return date(2000 + int(year), int(month), int(day))


def _number(text: str) -> Decimal:
    return Decimal(text.replace(" ", "").replace(" ", "").replace(",", "."))


def _signed(sign: str, text: str) -> Decimal:
    value = _number(text)
    return value if sign == "+" else -value


PDFTOTEXT_TIMEOUT = 60


def extract_text(pdf: Path) -> str:
    """Текст PDF с сохранением колонок. Нужен pdftotext из Poppler."""
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf), "-"],
            capture_output=True,
            encoding="utf-8",
            check=True,
            timeout=PDFTOTEXT_TIMEOUT,
        )
    except FileNotFoundError:
        raise KaspiError(
            "для импорта PDF нужен pdftotext из Poppler; "
            "на Windows: winget search poppler, затем перезапустите терминал"
        ) from None
    except subprocess.CalledProcessError as exc:
        reason = (exc.stderr or "").strip().splitlines()[-1:] or ["причина неизвестна"]
        raise KaspiError(f"файл не читается как PDF ({reason[0]})") from None
    except subprocess.TimeoutExpired:
        raise KaspiError("pdftotext разбирает файл слишком долго; это точно выписка?") from None
    except UnicodeDecodeError:
        raise KaspiError("текст из PDF не читается (не UTF-8)") from None
    return result.stdout


def parse_statement(text: str) -> Statement:
    period = _PERIOD.search(text)
    if not period:
        raise KaspiError("не похоже на выписку Kaspi: не найден период")
    summary: dict[str, Decimal] = {}
    available: list[Decimal] = []
    rows: list[KaspiRow] = []
    in_table = False
    for line in text.replace("\f", "\n").split("\n"):
        stripped = line.strip()
        if not in_table:
            match = _SUMMARY.match(line)
            if match:
                value = _signed(match[2], match[3])
                if match[1].startswith("Доступно на"):
                    available.append(value)
                else:
                    summary[match[1]] = value
            elif _HEADER.match(line):
                in_table = True
            continue
        if any(marker in stripped for marker in _END):
            break
        if not stripped or stripped.startswith(_SKIP) or _HEADER.match(line):
            continue
        match = _ROW.match(line)
        if match:
            details = " ".join(match[5].split())
            rows.append(KaspiRow(_date(match[1]), _signed(match[2], match[3]), match[4], details))
            continue
        foreign = _FOREIGN.match(line)
        # Строка без даты продолжает предыдущую операцию: сумма в исходной
        # валюте «(- 149,50 TRY)» или пояснение вроде «Курсовая разница».
        # Сумма в тенге в такой строке означает неразобранную операцию.
        if not rows or _DATED.match(line) or (not foreign and "₸" in line):
            raise KaspiError(f"не удалось разобрать строку выписки: {stripped}")
        extra = f"{foreign[1].strip()} {foreign[2]}" if foreign else stripped
        last = rows[-1]
        rows[-1] = replace(last, note=f"{last.note}; {extra}" if last.note else extra)
    if not in_table:
        raise KaspiError("не похоже на выписку Kaspi: не найдена таблица операций")
    if len(available) != 2:
        raise KaspiError("не найдены остатки на начало и конец периода")
    return Statement(
        period_from=_date(period[1]),
        period_to=_date(period[2]),
        opening=available[0],
        closing=available[1],
        summary=summary,
        rows=rows,
    )


def reconcile(st: Statement) -> list[str]:
    """Сверить строки с шапкой выписки. Пустой список означает, что всё сошлось."""
    problems = []
    total = st.opening + sum((r.amount for r in st.rows), Decimal("0"))
    if total != st.closing:
        problems.append(f"остаток на конец: по строкам {total}, в выписке {st.closing}")
    for operation, group in GROUPS.items():
        actual = sum((r.amount for r in st.rows if r.operation == operation), Decimal("0"))
        expected = st.summary.get(group)
        if expected is None:
            problems.append(f"в шапке нет строки «{group}»")
        elif actual != expected:
            problems.append(f"{group}: по строкам {actual}, в шапке {expected}")
    for group in UNSUPPORTED_GROUPS:
        if st.summary.get(group, Decimal("0")) != 0:
            problems.append(f"группа «{group}» пока не поддерживается")
    return problems


def same_account(e: Entry, account: str) -> bool:
    """Запись того же счёта. Запись без счёта (журнал до [accounts], перевод схемы 1)
    подходит любому: иначе её выписка встала бы в журнал второй раз."""
    return e.account in (account, "")


def _imported(entries: list[Entry], account: str) -> list[Entry]:
    return [e for e in entries if e.source == "kaspi-pdf" and same_account(e, account)]


def new_rows(st: Statement, entries: list[Entry], account: str = "") -> list[KaspiRow]:
    """Строки выписки, которых ещё нет в журнале на этом счёте, от старых к новым.

    Одинаковые строки сравниваются по количеству: три покупки по 500 ₸ в один
    день у одного продавца — это три операции, а не дубли. Такая же покупка на другом
    счёте — другая операция.
    """
    have = Counter(e.source_ref for e in _imported(entries, account))
    result = []
    for row in reversed(st.rows):
        if have[row.ref] > 0:
            have[row.ref] -= 1
        else:
            result.append(row)
    return result


def missing_entries(st: Statement, entries: list[Entry], account: str = "") -> list[Entry]:
    """Записи журнала из выписок Kaspi этого счёта за период, которых нет в новой выписке."""
    in_statement = Counter(row.ref for row in st.rows)
    result = []
    for e in _imported(entries, account):
        if not st.period_from <= e.date <= st.period_to:
            continue
        if in_statement[e.source_ref] > 0:
            in_statement[e.source_ref] -= 1
        else:
            result.append(e)
    return result


def to_entry(row: KaspiRow, rules: Rules, entry_id: int, account: str = "") -> Entry:
    common = dict(
        id=entry_id,
        account=account,
        date=row.date,
        currency="KZT",
        description=row.details,
        source="kaspi-pdf",
        source_ref=row.ref,
        note=row.note,
    )
    if row.operation == "Пополнение":
        if row.amount <= 0:
            raise KaspiError(f"пополнение с неположительной суммой: {row.ref}")
        return Entry(type="income", amount=row.amount, category="входящие переводы", **common)
    if row.operation == "Снятие":
        if row.amount >= 0:
            raise KaspiError(f"снятие с неотрицательной суммой: {row.ref}")
        return Entry(type="cash_withdrawal", amount=-row.amount, **common)
    if row.amount == 0:
        raise KaspiError(f"операция с нулевой суммой: {row.ref}")
    # Точное правило, запомненное пользователем, важнее категории по типу операции.
    category = (
        exact_category(row.details, rules)
        or _FIXED_CATEGORIES.get(row.operation)
        or categorize(row.details, rules)
    )
    # Покупка со знаком «+» (возврат, курсовая разница) даёт отрицательный расход.
    return Entry(type="expense", amount=-row.amount, category=category, **common)


def to_entries(rows: list[KaspiRow], rules: Rules, start_id: int, account: str = "") -> list[Entry]:
    return [to_entry(row, rules, start_id + i, account) for i, row in enumerate(rows)]
