"""Общие помощники тестов: папка данных с настройками и пустым журналом."""

import tempfile
from pathlib import Path

from ledger import journal
from ledger.datadir import DataDir
from ledger.settings import Settings, load_settings

MINIMAL = 'schema = 2\ncode = "."\n[owner]\ncurrency = "USDT"\ncountry = "KZ"\n'

# Один человек с расчётами: поведение, к которому привык журнал схемы 1.
ANYA = (
    MINIMAL + '[people.anya]\nname = "Аня"\nrole = "партнёр"\nsettlements = true\n'
    "default_share = 50\n"
)


def make_data(tmp: Path, toml: str = MINIMAL) -> DataDir:
    data = DataDir(Path(tmp))
    data.root.mkdir(parents=True, exist_ok=True)
    data.settings_file.write_text(toml, encoding="utf-8")
    journal.save(data.journal, [])
    return data


def make_settings(toml: str = ANYA) -> Settings:
    """Настройки из текста TOML, без папки данных."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.toml"
        path.write_text(toml, encoding="utf-8")
        return load_settings(path)
