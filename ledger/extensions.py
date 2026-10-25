"""Расширения из папки данных: импортёры, отчёты и правила налога.

Каждый `extensions/*.py`, кроме `test_*.py`, получает `register(api)`. Расширение не пишет журнал
само: импортёр возвращает строки, основа их проверяет и записывает под общим замком.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ledger.datadir import DataDir
from ledger.journal import IMPORTER_NAME, LedgerError, locked, replace_retrying
from ledger.settings import Settings

BUILTIN_REPORTS = ("balance", "categories", "debt", "tax")
BUILTIN_IMPORTERS = ("kaspi",)
CODE_ROOT = Path(__file__).resolve().parent.parent  # папка, где лежит ledger/
TESTS_TIMEOUT = 300


class ExtensionError(LedgerError):
    """Расширение отказало или нарушило договор с основой."""


@dataclass
class Loaded:
    importers: dict[str, Callable] = field(default_factory=dict)
    reports: dict[str, Callable] = field(default_factory=dict)
    tax_rules: dict[str, Callable] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    # («importer» | «report» | «tax_rule», имя) → имя файла расширения без .py
    owners: dict[tuple[str, str], str] = field(default_factory=dict)

    def owner(self, kind: str, name: str) -> str:
        return self.owners[(kind, name)]


class Api:
    """Что видит register(api). Регистрации одного файла принимаются только все вместе."""

    def __init__(self, settings: Settings, loaded: Loaded) -> None:
        self._settings = settings
        self._loaded = loaded
        self._staged: dict[tuple[str, str], Callable] = {}

    def _stage(self, kind: str, name: str, fn: Callable, taken: dict[str, Callable]) -> None:
        if not callable(fn):
            raise ExtensionError(f"{name}: передана не функция")
        if name in taken or (kind, name) in self._staged:
            raise ExtensionError(f"«{name}» уже зарегистрировано другим расширением")
        self._staged[(kind, name)] = fn

    def importer(self, name: str, parse: Callable) -> None:
        if not isinstance(name, str) or not IMPORTER_NAME.fullmatch(name):
            raise ExtensionError(
                f"имя импортёра {name!r}: латиница в нижнем регистре, цифры, _ и -"
            )
        if name in BUILTIN_IMPORTERS:
            raise ExtensionError(f"импортёр «{name}» встроен в основу")
        self._stage("importer", name, parse, self._loaded.importers)

    def report(self, name: str, fn: Callable) -> None:
        if not isinstance(name, str) or not name or name.startswith("-") or name.split() != [name]:
            raise ExtensionError(f"имя отчёта {name!r}: одно слово, не с дефиса")
        if name in BUILTIN_REPORTS:
            raise ExtensionError(f"отчёт «{name}» встроен в основу")
        self._stage("report", name, fn, self._loaded.reports)

    def tax_rule(self, book: str, rule: Callable) -> None:
        if not self._settings.has_books or book not in self._settings.books:
            raise ExtensionError(f"книга {book!r} не найдена в settings.toml")
        self._stage("tax_rule", book, rule, self._loaded.tax_rules)

    def commit(self, stem: str) -> None:
        targets = {
            "importer": self._loaded.importers,
            "report": self._loaded.reports,
            "tax_rule": self._loaded.tax_rules,
        }
        for (kind, name), fn in self._staged.items():
            targets[kind][name] = fn
            self._loaded.owners[(kind, name)] = stem


def extension_files(data: DataDir) -> list[Path]:
    if not data.extensions.is_dir():
        return []
    return sorted(p for p in data.extensions.glob("*.py") if not p.name.startswith("test_"))


def _load_one(path: Path, api: Api) -> None:
    name = f"budzheved_ext_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ExtensionError("файл не загружается как модуль Python")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses и pickle ищут модуль здесь
    writes_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # без __pycache__ в папке данных
    try:
        spec.loader.exec_module(module)
        register = getattr(module, "register", None)
        if not callable(register):
            raise ExtensionError("нет функции register(api)")
        register(api)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    finally:
        sys.dont_write_bytecode = writes_bytecode


def _describe(exc: BaseException) -> str:
    if isinstance(exc, LedgerError):
        return str(exc)
    if isinstance(exc, SyntaxError):
        return f"синтаксическая ошибка, строка {exc.lineno}: {exc.msg}"
    return f"{type(exc).__name__}: {exc}"


def load_extensions(data: DataDir) -> Loaded:
    """Загрузить все расширения. Сломанное выключается одно, с записью в errors.

    Код модуля и register(api) идут под той же охраной, что и вызовы расширения:
    запись мимо основы откатывается, а расширение выключается.
    """
    loaded = Loaded()
    for path in extension_files(data):
        api = Api(data.settings, loaded)
        try:
            guarded(data, path.name, _load_one, path, api)
        except ExtensionError as exc:
            cause = exc.__cause__
            loaded.errors.append(
                str(exc)
                if cause is None
                else f"расширение {path.name} выключено: {_describe(cause)}"
            )
            continue
        api.commit(path.stem)
    return loaded


def _watched(data: DataDir) -> list[Path]:
    return [data.journal, data.journal.with_suffix(".seq"), data.settings_file, data.merchants]


def _snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
    return {p: p.read_bytes() if p.exists() else None for p in paths}


def _write_bytes(path: Path, content: bytes) -> None:
    """Записать байты через временный файл рядом: сбой не оставит обрывок."""
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        replace_retrying(name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)
        raise


def _restore(before: dict[Path, bytes | None]) -> tuple[list[str], list[str]]:
    """Вернуть файлы к слепку. Возвращает имена изменённых файлов и тех, что вернуть
    не удалось (с причиной); сбой на одном файле не останавливает остальные."""
    changed: list[str] = []
    failed: list[str] = []
    for path, content in before.items():
        try:
            now = path.read_bytes() if path.exists() else None
            if now == content:
                continue
            changed.append(path.name)
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _write_bytes(path, content)
        except OSError as exc:
            failed.append(f"{path.name} ({exc.strerror or exc})")
    return changed, failed


def _not_restored(failed: list[str]) -> str:
    """Хвост отказа о файлах, которые вернуть не удалось: их возвращают из backups/ или git."""
    return f"; вернуть не удалось: {', '.join(failed)}" if failed else ""


def guarded(data: DataDir, name: str, fn: Callable, *args):
    """Вызвать функцию расширения под замком журнала и вернуть её результат.

    Журнал, счётчик номеров, настройки и правила продавцов сверяются побайтно до и после:
    изменение возвращается назад, а вызов отказывает.
    """
    with locked(data.journal):  # чтобы запись страницы в это время не приняли за расширение
        before = _snapshot(_watched(data))
        failure: BaseException | None = None
        try:
            result = fn(*args)
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - любая ошибка чужого кода
            failure = exc
        finally:
            changed, failed = _restore(before)
    if changed or failed:
        raise ExtensionError(
            f"расширение {name} пыталось изменить {', '.join(changed)} мимо основы и выключено"
            + _not_restored(failed)
        )
    if failure is not None:
        raise ExtensionError(f"расширение {name}: {failure}") from failure
    return result


@dataclass
class ExtensionTests:
    ok: bool
    output: str
    # «тесты расширения X изменили journal.csv мимо основы»: изменения уже возвращены
    tampered: list[str] = field(default_factory=list)


def run_tests(data: DataDir) -> ExtensionTests | None:
    """Тесты расширений `extensions/test_*.py`, каждый файл в отдельном процессе; None,
    если тестов нет.

    Журнал, счётчик, настройки и правила продавцов сверяются до и после каждого файла:
    изменение возвращается, а проверка не проходит. Замок журнала на время тестов
    не берётся: тест, который зовёт операции основы, иначе ждал бы сам себя.
    """
    files = sorted(data.extensions.glob("test_*.py")) if data.extensions.is_dir() else []
    if not files:
        return None
    results = [_run_test_file(data, path) for path in files]
    return ExtensionTests(
        all(r.ok for r in results),
        "\n".join(r.output for r in results if r.output),
        [line for r in results for line in r.tampered],
    )


def _run_test_file(data: DataDir, path: Path) -> ExtensionTests:
    env = {
        **os.environ,
        "PYTHONPATH": str(CODE_ROOT),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        str(data.extensions),
        "-p",
        path.name,
    ]
    before = _snapshot(_watched(data))
    try:
        done = subprocess.run(
            command,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=TESTS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        done = None
    finally:
        changed, failed = _restore(before)
    tampered = []
    if changed or failed:
        owner = path.stem.removeprefix("test_")
        tampered.append(
            f"тесты расширения {owner} изменили {', '.join(changed)} мимо основы"
            + (_not_restored(failed) or "; изменения возвращены")
        )
    if done is None:
        raise LedgerError(
            "; ".join([f"тесты {path.name} идут дольше {TESTS_TIMEOUT} с", *tampered])
        )
    ok = done.returncode == 0 and not tampered
    return ExtensionTests(ok, (done.stdout + done.stderr).rstrip(), tampered)
