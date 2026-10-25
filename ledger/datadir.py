"""Папка данных: всё, что принадлежит одному человеку, лежит в одной папке."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from ledger.settings import Settings, load_settings


@dataclass(frozen=True)
class DataDir:
    root: Path

    @property
    def journal(self) -> Path:
        return self.root / "journal.csv"

    @property
    def merchants(self) -> Path:
        return self.root / "merchants.csv"

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.toml"

    @property
    def receipts(self) -> Path:
        return self.root / "receipts"

    @property
    def extensions(self) -> Path:
        return self.root / "extensions"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @cached_property
    def settings(self) -> Settings:
        """Настройки читаются при первом обращении; нет файла или ошибка в нём: SettingsError."""
        return load_settings(self.settings_file)
