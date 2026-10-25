#!/usr/bin/env python3
"""Вход папки кода: `python3 budget.py --data ПАПКА init` и любые команды с --data.

Файл читается и старым Python (3.6+), чтобы сказать, что нужен новый, а не упасть.
"""

import sys

NEED = (3, 11)
OLD_PYTHON = "Нужен Python 3.11 или новее. На Windows: winget install Python.Python.3.13"

if __name__ == "__main__":
    if sys.version_info < NEED:
        print(OLD_PYTHON, file=sys.stderr)
        sys.exit(1)
    from ledger import cli

    sys.exit(cli.main())
