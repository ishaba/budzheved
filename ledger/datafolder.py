"""Новая папка данных: команда init."""

from __future__ import annotations

import shutil
from pathlib import Path

from ledger import agents, journal
from ledger.datadir import DataDir
from ledger.journal import LedgerError
from ledger.merchants import save_rules
from ledger.settings import SCHEMA, is_currency

LAUNCHER = Path(__file__).parent / "templates" / "launcher.py"
GITIGNORE = "*.lock\n*.tmp\n__pycache__/\nbackups/\n"


def _toml_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _settings_text(code_root: Path, currency: str, country: str) -> str:
    return (
        f"schema = {SCHEMA}\n"
        f"code = {_toml_string(code_root.as_posix())}\n"
        "\n[owner]\n"
        f"currency = {_toml_string(currency)}\n"
        f"country = {_toml_string(country)}\n"
    )


def _create(path: Path, text: str) -> None:
    """Файл, которого ещё нет; уже лежащий в папке не перезаписывается."""
    if not path.exists():
        path.write_text(text, encoding="utf-8", newline="")


def init_folder(root: Path, code_root: Path, currency: str, country: str) -> DataDir:
    """Создать папку данных: настройки, пустой журнал, запуск budget.py и правила агента."""
    data = DataDir(root)
    if data.settings_file.exists():
        raise LedgerError(f"папка уже настроена: в {root} есть settings.toml")
    if not is_currency(currency):
        raise LedgerError(
            f"валюта {currency!r}: нужны три заглавные латинские буквы (KZT) или USDT"
        )
    country = country.strip().upper()
    if not country:
        raise LedgerError("страна не указана: --country, например KZ")
    root.mkdir(parents=True, exist_ok=True)
    for folder in (data.receipts, data.extensions, data.backups):
        folder.mkdir(exist_ok=True)
    if not data.journal.exists():
        journal.save(data.journal, [])
    if not data.merchants.exists():
        save_rules(data.merchants, [])
    launcher = root / "budget.py"
    if not launcher.exists():
        shutil.copyfile(LAUNCHER, launcher)
    _create(root / ".gitignore", GITIGNORE)
    _create(root / "CLAUDE.md", "@AGENTS.md\n")
    # Настройки последними: если init упал до них, повторный init доделает папку.
    # После них повторный init отказывает; AGENTS.md тогда перестраивает команда agents.
    data.settings_file.write_text(
        _settings_text(code_root, currency, country), encoding="utf-8", newline=""
    )
    agents.write(data)
    return data
