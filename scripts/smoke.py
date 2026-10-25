"""Проверка первого запуска так, как его делает человек: архив кода без git, init, запись, отчёты.

Запуск: python scripts/smoke.py <папка кода>. Печатает «smoke ok» или выходит с кодом 1.
Только стандартная библиотека; дочерние процессы идут тем же интерпретатором без git в PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

# Чего нет в архиве выпуска: история git и следы разработки.
SKIP = shutil.ignore_patterns(
    ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".claude", ".superpowers"
)
EXPECTED_BALANCE = "KZT: -1 500,00"
TIMEOUT = 120  # с на каждую команду


class SmokeError(Exception):
    pass


def env_without_git() -> dict[str, str]:
    """Окружение детей: PATH без каталогов, где лежит git.

    PYTHONIOENCODING не выставляется: агент его забывает, и budget.py должен сам писать UTF-8.
    """
    names = ("git.exe", "git") if os.name == "nt" else ("git",)
    kept = [
        folder
        for folder in os.environ.get("PATH", "").split(os.pathsep)
        if folder and not any((Path(folder) / name).is_file() for name in names)
    ]
    env = {**os.environ, "PATH": os.pathsep.join(kept), "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("PYTHONPATH", "PYTHONIOENCODING", "PYTHONUTF8"):
        env.pop(name, None)
    return env


def run(step: str, args: list[str], cwd: Path, env: dict[str, str]) -> str:
    """Выполнить шаг; код не 0 — SmokeError. Вывод детей читается как UTF-8 строго:
    битая кодировка — тоже сбой."""
    done = subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=TIMEOUT,
    )
    raw = done.stdout + done.stderr
    try:
        output = raw.decode("utf-8").rstrip()
    except UnicodeDecodeError as exc:
        print(f"$ {step}\n{raw.decode('utf-8', errors='replace').rstrip()}\n")
        raise SmokeError(f"{step}: вывод не в UTF-8 ({exc})") from None
    print(f"$ {step}\n{output}\n")
    if done.returncode != 0:
        raise SmokeError(f"{step}: код выхода {done.returncode}")
    return output


def smoke(code_dir: Path, tmp: Path) -> None:
    code = tmp / "Код Бюджеведа"
    shutil.copytree(code_dir, code, ignore=SKIP)
    data = tmp / "Мои деньги"
    env = env_without_git()
    if shutil.which("git", path=env["PATH"]):
        raise SmokeError("git остался в PATH дочерних процессов")

    run(
        "init",
        [str(code / "budget.py"), "--data", str(data), "init", "--currency", "KZT"],
        tmp,
        env,
    )
    month = date.today().strftime("%Y-%m")
    steps = [
        ("add", ["add", "--amount", "1500", "--currency", "KZT", "--desc", "кофе ☕"]),
        ("report balance", ["report", "balance"]),
        (f"report categories --month {month}", ["report", "categories", "--month", month]),
        ("check", ["check"]),
    ]
    outputs = {step: run(step, ["budget.py", *args], data, env) for step, args in steps}

    if EXPECTED_BALANCE not in outputs["report balance"]:
        raise SmokeError(f"report balance: нет строки {EXPECTED_BALANCE!r}")
    if "кофе ☕" not in outputs["add"]:
        raise SmokeError("add: описание с кириллицей и эмодзи пришло битым")
    if not list((data / "backups").glob("journal-*.csv")):
        raise SmokeError("в backups/ нет копии журнала")


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    if len(argv) != 1:
        print("Запуск: python scripts/smoke.py <папка кода>", file=sys.stderr)
        return 2
    code_dir = Path(argv[0]).resolve()
    if not (code_dir / "ledger" / "cli.py").is_file():
        print(f"smoke failed: в {code_dir} нет ledger/cli.py", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        try:
            smoke(code_dir, Path(tmp))
        except (SmokeError, subprocess.TimeoutExpired) as exc:
            print(f"smoke failed: {exc}", file=sys.stderr)
            return 1
    print("smoke ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
