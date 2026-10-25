"""Команда update: новая версия кода через git или архив последнего выпуска на GitHub."""

from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from ledger.datadir import DataDir
from ledger.journal import MIGRATE_HINT, LedgerError, replace_retrying

RELEASES_API = "https://api.github.com/repos/ishaba/budzheved/releases/latest"
TIMEOUT = 60  # с на каждый запрос к GitHub
COMMAND_TIMEOUT = 300  # с на git pull и на проверки новым кодом
_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "budzheved-update"}
_BUSY = (
    "папку кода {code} держит другая программа. Закройте страницу ui и другие окна "
    "Бюджеведа (терминал, проводник, редактор в этой папке) и повторите update"
)


def run(code_root: Path, data: DataDir, fetch=urllib.request.urlopen) -> str:
    """Обновить код в `code_root` и перестроить AGENTS.md папки данных уже новым кодом.

    Папка под git обновляется `git pull --ff-only`, иначе ставится архив последнего выпуска.
    Любой сбой до замены оставляет прежний код как был.
    """
    code_root = Path(code_root).resolve()
    if data.root.resolve().is_relative_to(code_root):
        raise LedgerError(
            f"папка данных лежит внутри папки кода ({code_root}); "
            "перенесите её в другое место, иначе обновление её заменит"
        )
    if (code_root / ".git").exists():
        how = _git_pull(code_root)
    else:
        how = _install_release(code_root, fetch)
    lines = [f"Код обновлён: {how}.", *_after_update(code_root, data)]
    return "\n".join(
        [*lines, "Страница работает на прежнем коде: перезапустите ui, если он открыт."]
    )


def _git_pull(code_root: Path) -> str:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",  # без вопросов о пароле: команда не должна зависнуть
        "GIT_CEILING_DIRECTORIES": str(code_root.parent),  # только свой репозиторий
    }
    try:
        done = subprocess.run(
            ["git", "-C", str(code_root), "pull", "--ff-only"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=COMMAND_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LedgerError(f"git pull не выполнен, код не изменён: {exc}") from None
    if done.returncode != 0:
        detail = done.stderr.strip() or done.stdout.strip()
        raise LedgerError(f"git pull --ff-only не прошёл, код не изменён: {detail}")
    said = done.stdout.strip().splitlines()
    return f"git pull ({said[-1]})" if said else "git pull"


def _download(fetch, url: str) -> bytes:
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with fetch(request, timeout=TIMEOUT) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise LedgerError(f"GitHub ответил {status} на {url}; код не изменён")
            return response.read()
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise LedgerError(f"не удалось скачать {url}: {exc}; код не изменён") from None


def _install_release(code_root: Path, fetch) -> str:
    try:
        release = json.loads(_download(fetch, RELEASES_API))
    except ValueError:
        raise LedgerError("GitHub прислал не JSON о выпуске; код не изменён") from None
    url = release.get("zipball_url") if isinstance(release, dict) else None
    if not isinstance(url, str) or not url:
        raise LedgerError("в ответе GitHub нет zipball_url; код не изменён")
    archive = _download(fetch, url)
    # Рядом с кодом, на том же диске: переименование тогда мгновенное и не копирует файлы.
    staging = Path(tempfile.mkdtemp(dir=code_root.parent, prefix=f".{code_root.name}.new-"))
    try:
        new_root = _unpack(archive, staging / "unpacked")
        _require_imports(new_root)
        _swap(code_root, new_root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    tag = release.get("tag_name")
    return f"выпуск {tag}" if isinstance(tag, str) and tag else "последний выпуск"


def _unpack(archive: bytes, target: Path) -> Path:
    """Распаковать архив и снять единственную верхнюю папку (<owner>-<repo>-<sha>/ у GitHub)."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            zipped.extractall(target)  # extractall сам отбрасывает «..» и абсолютные пути
    except (zipfile.BadZipFile, OSError) as exc:
        raise LedgerError(f"архив выпуска не читается: {exc}; код не изменён") from None
    inside = list(target.iterdir()) if target.is_dir() else []
    root = inside[0] if len(inside) == 1 and inside[0].is_dir() else target
    if not (root / "ledger" / "cli.py").is_file():
        raise LedgerError("в архиве выпуска нет ledger/cli.py; код не изменён")
    return root


def _python(code_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Python с кодом из `code_root`: текущая папка в sys.path идёт первой."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, *args],
        cwd=code_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=COMMAND_TIMEOUT,
    )


def _last_line(done: subprocess.CompletedProcess[str]) -> str:
    lines = (done.stderr or done.stdout).strip().splitlines()
    return lines[-1] if lines else f"код выхода {done.returncode}"


def _require_imports(new_root: Path) -> None:
    try:
        done = _python(new_root, ["-c", "import ledger.cli"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LedgerError(f"новый код не проверен: {exc}; код не изменён") from None
    if done.returncode != 0:
        raise LedgerError(f"новый код не запускается: {_last_line(done)}; код не изменён")


def _swap(code_root: Path, new_root: Path) -> None:
    """Старый код уходит в новую папку отката рядом, новый встаёт на его место, папка
    отката удаляется. Сбой или Ctrl+C посреди замены — старый код на месте.

    Папку отката создаёт этот update под неповторимым именем: чужую папку рядом
    (в том числе папку данных с похожим именем) он не трогает и не удаляет.
    """
    busy = _BUSY.format(code=code_root)
    try:
        rollback = Path(tempfile.mkdtemp(dir=code_root.parent, prefix=f".{code_root.name}.old-"))
    except OSError:
        raise LedgerError(busy) from None
    old = rollback / code_root.name
    try:
        replace_retrying(code_root, old)
    except BaseException as exc:
        _remove_empty(rollback)
        if isinstance(exc, OSError):
            raise LedgerError(busy) from None
        raise
    try:
        replace_retrying(new_root, code_root)
    except BaseException as exc:
        try:
            replace_retrying(old, code_root)
        except OSError:
            raise LedgerError(
                f"{busy}. Прежний код сейчас в {old}: переименуйте папку обратно в {code_root}"
            ) from None
        _remove_empty(rollback)
        if isinstance(exc, OSError):
            raise LedgerError(busy) from None
        raise
    # На Windows удаление может не пройти, пока файл открыт: тогда папка отката остаётся,
    # её можно удалить руками.
    shutil.rmtree(rollback, ignore_errors=True)


def _remove_empty(folder: Path) -> None:
    """Убрать пустую папку отката; непустую (там прежний код) не трогать."""
    try:
        folder.rmdir()
    except OSError:
        pass


def _after_update(code_root: Path, data: DataDir) -> list[str]:
    """AGENTS.md и сверка схемы — новым кодом в отдельном процессе: в этом уже загружен старый."""
    lines = []
    try:
        agents = _python(code_root, ["-m", "ledger.cli", "--data", str(data.root), "agents"])
        problem = None if agents.returncode == 0 else _last_line(agents)
    except (OSError, subprocess.TimeoutExpired) as exc:
        problem = str(exc)
    if problem is None:
        lines.append("AGENTS.md перестроен новым кодом.")
    else:
        lines.append(f"Внимание: AGENTS.md не перестроен ({problem}); выполните budget.py agents")
    try:
        done = _python(code_root, ["-c", "from ledger.settings import SCHEMA; print(SCHEMA)"])
        known = int(done.stdout.strip())
        schema = data.settings.schema
    except (OSError, subprocess.TimeoutExpired, ValueError, LedgerError):
        lines.append("Внимание: схему журнала сверить не удалось; выполните budget.py check")
        return lines
    if schema < known:
        if schema == 1:
            lines.append(MIGRATE_HINT)
        else:
            lines.append(
                f"журнал схемы {schema}, а код теперь знает схему {known}: "
                "выполните budget.py migrate"
            )
    return lines
