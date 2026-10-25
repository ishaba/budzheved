"""AGENTS.md папки данных: блок основы по настройкам, раздел человека не трогается."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from ledger import backups, extensions
from ledger.datadir import DataDir
from ledger.journal import LedgerError, atomic_writer
from ledger.settings import Settings

BEGIN = "<!-- budzheved:begin -->"
END = "<!-- budzheved:end -->"
TEMPLATE = Path(__file__).parent / "templates" / "agents_core.md"
OWN_SECTION = "\n## Моё\n\nДоговорённости, «к чему вернуться», мои расширения.\n"
_IF = re.compile(r"<!-- if (\w+) -->")
_ENDIF = "<!-- endif -->"


def run_command(platform: str) -> str:
    """Как запускать budget.py папки данных: на Windows через py, иначе python3."""
    return "py budget.py" if platform == "win32" else "python3 budget.py"


def _people(s: Settings) -> str:
    return "\n".join(
        f"- {p.name} ({p.key}), доля по умолчанию {p.default_share}%" for p in s.settlement_people()
    )


def _accounts(s: Settings) -> str:
    lines = []
    for a in s.accounts.values():
        parts = [", ".join(a.currencies)]
        if s.has_books:
            parts.append(f"книга «{a.book}»")
        if a.importer:
            parts.append(f"импорт {a.importer}")
        lines.append(f"- `{a.key}`: {', '.join(parts)}")
    return "\n".join(lines)


def _conditional(template: str, flags: dict[str, bool]) -> str:
    """Убрать части `<!-- if флаг -->` … `<!-- endif -->` с выключенным флагом; можно вкладывать."""
    shown: list[bool] = []
    kept = []
    for number, line in enumerate(template.splitlines(keepends=True), 1):
        stripped = line.strip()
        opened = _IF.fullmatch(stripped)
        if opened:
            if opened.group(1) not in flags:
                raise ValueError(f"agents_core.md, строка {number}: неизвестный флаг {stripped}")
            shown.append(flags[opened.group(1)])
        elif stripped == _ENDIF:
            if not shown:
                raise ValueError(f"agents_core.md, строка {number}: лишний {_ENDIF}")
            shown.pop()
        elif all(shown):
            kept.append(line)
    if shown:
        raise ValueError("agents_core.md: не закрыт <!-- if … -->")
    return "".join(kept)


def render_core(
    s: Settings, platform: str, *, in_git: bool = False, has_extensions: bool = False
) -> str:
    """Блок основы вместе с маркерами, по включённым частям настроек.

    in_git и has_extensions знает только папка данных; их передаёт write.
    """
    flags = {
        "settlements": s.has_settlements,
        "accounts": s.has_accounts,
        "books": s.has_books,
        "tax": bool(s.tax),
        "extensions": has_extensions,
        "nosettlements": not s.has_settlements,
        "manysettlements": len(s.settlement_people()) > 1,
        "people": bool(s.people),
        "nogit": not in_git,
    }
    text = _conditional(TEMPLATE.read_text(encoding="utf-8"), flags)
    values = {
        "{run}": run_command(platform),
        "{people}": _people(s),
        "{accounts}": _accounts(s),
        "{currency}": s.currency,
    }
    text = re.sub(r"\{(run|people|accounts|currency)\}", lambda m: values[m.group(0)], text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
    return f"{BEGIN}\n{text}\n{END}\n"


def write(data: DataDir, platform: str = sys.platform) -> None:
    """Перестроить блок основы в AGENTS.md; текст вне маркеров остаётся побайтно."""
    path = data.root / "AGENTS.md"
    block = render_core(
        data.settings,
        platform,
        in_git=backups.in_git(data.root),
        has_extensions=bool(extensions.extension_files(data)),
    )
    if not path.exists():
        text = block + OWN_SECTION
    else:
        with path.open(encoding="utf-8", newline="") as fh:
            old = fh.read()
        begin, end = old.find(BEGIN), old.find(END)
        newline = "\r\n" if "\r\n" in old else "\n"  # файл с CRLF получает блок с CRLF
        block = block.replace("\n", newline)
        if begin == -1 and end == -1:
            text = block + newline + old
        elif begin == -1 or end == -1 or end < begin or old.count(BEGIN) + old.count(END) != 2:
            raise LedgerError(
                f"AGENTS.md: маркеры {BEGIN} и {END} повреждены; "
                "верните их по одному, begin выше end, и выполните "
                f"{run_command(platform)} agents"
            )
        else:
            text = old[:begin] + block.removesuffix(newline) + old[end + len(END) :]
    with atomic_writer(path) as fh:
        fh.write(text)
