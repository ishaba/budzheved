#!/usr/bin/env python3
"""Запуск Бюджеведа из папки данных: код берётся по пути `code` из settings.toml рядом.

Файл читается и старым Python (3.6+), чтобы найти новый и перезапуститься им.
"""

import os
import shutil
import subprocess
import sys

NEED = (3, 11)
OLD_PYTHON = "Нужен Python 3.11 или новее. На Windows: winget install Python.Python.3.13"
CHECK = "import sys; sys.exit(sys.version_info < (3, 11))"


def _candidates():
    if os.name == "nt" and shutil.which("py"):
        for version in ("3.13", "3.12", "3.11"):
            yield [shutil.which("py"), "-" + version]
    for name in ("python3.13", "python3.12", "python3.11"):
        found = shutil.which(name)
        if found:
            yield [found]


def _works(cmd):
    try:
        done = subprocess.call(
            cmd + ["-c", CHECK], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except OSError:
        return False
    return done == 0


def _restart():
    for cmd in _candidates():
        if _works(cmd):
            sys.exit(subprocess.call(cmd + [os.path.abspath(__file__)] + sys.argv[1:]))
    print(OLD_PYTHON, file=sys.stderr)
    sys.exit(1)


def main():
    if sys.version_info < NEED:
        _restart()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    import tomllib
    from pathlib import Path

    folder = Path(__file__).resolve().parent
    try:
        with open(folder / "settings.toml", "rb") as fh:
            code = tomllib.load(fh).get("code", "")
    except FileNotFoundError:
        print(f"Ошибка: рядом с budget.py нет settings.toml ({folder})", file=sys.stderr)
        return 1
    except tomllib.TOMLDecodeError as exc:
        print(f"Ошибка: settings.toml: {exc}. В путях пишите / вместо \\", file=sys.stderr)
        return 1
    root = (folder / str(code)).resolve()
    if not code or not (root / "ledger" / "cli.py").is_file():
        print(f"Код Бюджеведа не найден по пути из settings.toml: {code}", file=sys.stderr)
        return 1
    sys.path.insert(0, str(root))
    from ledger import cli

    return cli.main(["--data", str(folder)] + sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
