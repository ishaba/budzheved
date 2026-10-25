import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

from ledger import agents, cli, update
from ledger.extensions import CODE_ROOT
from ledger.journal import LedgerError
from tests.helpers import MINIMAL, make_data

ZIPBALL = "https://example.invalid/zipball/v2"
TOP = "owner-budzheved-abc1234/"

# Новый код-заглушка: команда agents пишет метку, чтобы было видно, чей код её выполнил.
STUB_CLI = """\
import sys
from pathlib import Path


def main(argv):
    data = Path(argv[argv.index("--data") + 1])
    (data / "AGENTS.md").write_text("new " + argv[-1], encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
"""


class Response:
    def __init__(self, body: bytes, status: int = 200):
        self.body, self.status = body, status

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(TOP, "")
        for name, text in files.items():
            archive.writestr(TOP + name, text)
    return buffer.getvalue()


def real_code_zip() -> bytes:
    """Архив как у GitHub: настоящий ledger/ этого репозитория под одной верхней папкой."""
    files = {}
    for path in sorted((CODE_ROOT / "ledger").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            files[path.relative_to(CODE_ROOT).as_posix()] = path.read_text(encoding="utf-8")
    return zip_bytes(files)


def stub_zip(schema: int = 2, cli_text: str = STUB_CLI) -> bytes:
    return zip_bytes(
        {
            "ledger/__init__.py": "",
            "ledger/cli.py": cli_text,
            "ledger/settings.py": f"SCHEMA = {schema}\n",
            "README.md": "новый\n",
        }
    )


class Fetch:
    """Подменённая сеть: сначала ответ releases/latest, потом архив."""

    def __init__(self, archive: bytes, release: dict | None = None, status: int = 200):
        self.archive = archive
        self.release = {"zipball_url": ZIPBALL} if release is None else release
        self.status = status
        self.urls: list[str] = []

    def __call__(self, request, timeout=None):
        url = getattr(request, "full_url", request)
        self.urls.append(url)
        if url == update.RELEASES_API:
            return Response(json.dumps(self.release).encode("utf-8"), self.status)
        if url == ZIPBALL:
            return Response(self.archive)
        raise AssertionError(f"неожиданный адрес {url}")


class UpdateArchiveTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()  # на macOS /var ведёт в /private/var
        self.code = self.tmp / "budzheved"
        (self.code / "ledger").mkdir(parents=True)
        (self.code / "ledger" / "__init__.py").write_text("", encoding="utf-8")
        (self.code / "ledger" / "cli.py").write_text("OLD = True\n", encoding="utf-8")
        (self.code / "old_only.txt").write_text("старое", encoding="utf-8")
        self.data = make_data(self.tmp / "Мои деньги", MINIMAL)

    def old_code_intact(self):
        self.assertEqual(
            (self.code / "ledger" / "cli.py").read_text(encoding="utf-8"), "OLD = True\n"
        )
        self.assertTrue((self.code / "old_only.txt").exists())
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["budzheved", "Мои деньги"])

    def test_installs_the_new_code_and_rebuilds_agents_with_it(self):
        fetch = Fetch(real_code_zip())
        message = update.run(self.code, self.data, fetch)
        self.assertEqual(fetch.urls, [update.RELEASES_API, ZIPBALL])
        self.assertTrue((self.code / "ledger" / "cli.py").is_file())
        self.assertNotIn("OLD", (self.code / "ledger" / "cli.py").read_text(encoding="utf-8"))
        self.assertFalse((self.code / "old_only.txt").exists())
        self.assertFalse((self.code / TOP).exists())  # верхняя папка архива снята
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["budzheved", "Мои деньги"])
        text = (self.data.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(agents.BEGIN, text)
        self.assertIn("Код обновлён", message)
        self.assertNotIn("migrate", message)

    def test_agents_are_rebuilt_by_the_new_code(self):
        update.run(self.code, self.data, Fetch(stub_zip()))
        self.assertEqual((self.data.root / "AGENTS.md").read_text(encoding="utf-8"), "new agents")
        self.assertEqual((self.code / "README.md").read_text(encoding="utf-8"), "новый\n")

    def test_newer_schema_in_the_code_asks_for_migrate(self):
        message = update.run(self.code, self.data, Fetch(stub_zip(schema=3)))
        self.assertIn("migrate", message)
        self.assertIn("схемы 2", message)

    def test_archive_without_ledger_is_refused_and_the_old_code_stays(self):
        archive = zip_bytes({"README.md": "без кода\n"})
        with self.assertRaises(LedgerError) as caught:
            update.run(self.code, self.data, Fetch(archive))
        self.assertIn("ledger/cli.py", str(caught.exception))
        self.old_code_intact()

    def test_new_code_that_does_not_import_is_refused(self):
        with self.assertRaises(LedgerError):
            update.run(self.code, self.data, Fetch(stub_zip(cli_text="def broken(:\n")))
        self.old_code_intact()

    def test_network_problems_change_nothing(self):
        def offline(request, timeout=None):
            raise urllib.error.URLError("нет сети")

        cases = {
            "offline": offline,
            "status": Fetch(stub_zip(), status=404),
            "no zipball": Fetch(stub_zip(), release={"tag_name": "v2"}),
            "not json": lambda request, timeout=None: Response(b"<html>"),
            "bad zip": Fetch(b"not a zip"),
        }
        for name, fetch in cases.items():
            with self.subTest(name), self.assertRaises(LedgerError):
                update.run(self.code, self.data, fetch)
            self.old_code_intact()

    def test_locked_folder_on_windows_is_refused_with_advice(self):
        with mock.patch("ledger.update.replace_retrying", side_effect=PermissionError(5, "в")):
            with self.assertRaises(LedgerError) as caught:
                update.run(self.code, self.data, Fetch(stub_zip()))
        self.assertIn("ui", str(caught.exception))
        self.old_code_intact()

    def test_failed_second_rename_puts_the_old_code_back(self):
        real = update.replace_retrying
        calls = []

        def flaky(src, dst):
            calls.append((Path(src), Path(dst)))
            if len(calls) == 2:
                raise PermissionError(5, "занято")
            real(src, dst)

        with mock.patch("ledger.update.replace_retrying", side_effect=flaky):
            with self.assertRaises(LedgerError):
                update.run(self.code, self.data, Fetch(stub_zip()))
        self.assertEqual(calls[0][0], self.code)
        self.assertEqual(calls[0][1].parent.parent, self.tmp)
        self.assertTrue(calls[0][1].parent.name.startswith(".budzheved.old-"))
        self.old_code_intact()

    def test_interrupt_during_the_swap_puts_the_old_code_back_and_reraises(self):
        real = update.replace_retrying
        calls = []

        def interrupted(src, dst):
            calls.append(src)
            if len(calls) == 2:
                raise KeyboardInterrupt
            real(src, dst)

        with mock.patch("ledger.update.replace_retrying", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                update.run(self.code, self.data, Fetch(stub_zip()))
        self.old_code_intact()

    def test_interrupt_after_the_new_code_is_in_place_says_the_update_is_done(self):
        real = update.replace_retrying
        calls = []

        def interrupted_after(src, dst):
            calls.append(src)
            real(src, dst)
            if len(calls) == 2:
                raise KeyboardInterrupt  # Ctrl+C пришёл, когда новый код уже встал на место

        with mock.patch("ledger.update.replace_retrying", side_effect=interrupted_after):
            with self.assertRaises(LedgerError) as caught:
                update.run(self.code, self.data, Fetch(stub_zip()))
        message = str(caught.exception)
        self.assertIn("переименовывать ничего не нужно", message)
        self.assertNotIn("переименуйте", message)
        self.assertEqual((self.code / "README.md").read_text(encoding="utf-8"), "новый\n")
        self.assertEqual(len(calls), 2)  # назад старый код не возвращали
        self.assertEqual(
            sorted(p.name for p in self.tmp.iterdir()), ["budzheved", "Мои деньги"]
        )  # папка отката убрана

    def test_folders_named_like_the_old_code_are_never_deleted(self):
        for name in ("budzheved.old", ".budzheved.old-keep"):
            folder = make_data(self.tmp / name, MINIMAL)
            data = folder if name == "budzheved.old" else self.data
            with self.subTest(name):
                update.run(self.code, data, Fetch(stub_zip()))
                self.assertTrue(folder.journal.exists())
        self.assertEqual(
            sorted(p.name for p in self.tmp.iterdir()),
            [".budzheved.old-keep", "budzheved", "budzheved.old", "Мои деньги"],
        )

    def test_success_reminds_to_restart_the_page(self):
        message = update.run(self.code, self.data, Fetch(stub_zip()))
        self.assertIn("перезапустите ui, если он открыт", message)

    def test_data_folder_inside_the_code_is_refused(self):
        inside = make_data(self.code / "данные", MINIMAL)
        fetch = Fetch(stub_zip())
        with self.assertRaises(LedgerError):
            update.run(self.code, inside, fetch)
        self.assertEqual(fetch.urls, [])
        self.assertTrue(inside.journal.exists())


class UpdateGitTest(unittest.TestCase):
    def test_git_failure_is_refused_with_its_message_and_no_network(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        code = Path(tmp.name) / "budzheved"
        (code / ".git").mkdir(parents=True)  # не репозиторий: pull откажет
        data = make_data(Path(tmp.name) / "Мои деньги", MINIMAL)
        fetch = Fetch(stub_zip())
        with self.assertRaises(LedgerError) as caught:
            update.run(code, data, fetch)
        self.assertIn("git", str(caught.exception))
        self.assertEqual(fetch.urls, [])
        self.assertFalse((data.root / "AGENTS.md").exists())


def git(*args, cwd: Path) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=Аня", "-c", "user.email=anya@example.invalid", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return done.stdout


@unittest.skipUnless(shutil.which("git"), "нет git")
class UpdateGitPullTest(unittest.TestCase):
    def test_pull_brings_the_new_upstream_commit_and_rebuilds_agents(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        upstream, author, code = root / "upstream.git", root / "author", root / "budzheved"
        git("init", "--bare", "-b", "main", str(upstream), cwd=root)
        git("clone", str(upstream), str(author), cwd=root)
        (author / "ledger").mkdir()
        (author / "ledger" / "__init__.py").write_text("", encoding="utf-8")
        (author / "ledger" / "cli.py").write_text(STUB_CLI, encoding="utf-8")
        (author / "ledger" / "settings.py").write_text("SCHEMA = 2\n", encoding="utf-8")
        git("add", ".", cwd=author)
        git("commit", "-m", "первый", cwd=author)
        git("push", "origin", "HEAD:main", cwd=author)
        git("clone", "-b", "main", str(upstream), str(code), cwd=root)
        (author / "README.md").write_text("новое\n", encoding="utf-8")
        git("add", ".", cwd=author)
        git("commit", "-m", "второй", cwd=author)
        git("push", "origin", "HEAD:main", cwd=author)
        data = make_data(root / "Мои деньги", MINIMAL)
        fetch = Fetch(stub_zip())
        message = update.run(code, data, fetch)
        self.assertEqual((code / "README.md").read_text(encoding="utf-8"), "новое\n")
        self.assertEqual(fetch.urls, [])
        self.assertIn("git pull", message)
        self.assertEqual((data.root / "AGENTS.md").read_text(encoding="utf-8"), "new agents")


class UpdateCliTest(unittest.TestCase):
    def test_update_command_runs_update_for_this_code(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = make_data(Path(tmp.name) / "Мои деньги", MINIMAL)
        out = io.StringIO()
        with (
            mock.patch("ledger.update.run", return_value="Код обновлён: тест") as run,
            contextlib.redirect_stdout(out),
        ):
            code = cli.main(["--data", str(data.root), "update"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], CODE_ROOT)
        self.assertEqual(run.call_args.args[1].root, data.root.resolve())
        self.assertIn("Код обновлён: тест", out.getvalue())

    def test_update_runs_even_when_settings_are_newer_than_the_code(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = make_data(Path(tmp.name) / "Мои деньги", MINIMAL.replace("schema = 2", "schema = 9"))
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("ledger.update.run", return_value="Код обновлён: тест") as run,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(cli.main(["--data", str(data.root), "update"]), 0, err.getvalue())
            self.assertEqual(cli.main(["--data", str(data.root), "check"]), 1)
        self.assertEqual(run.call_count, 1)
        self.assertIn("обновите", err.getvalue())


if __name__ == "__main__":
    unittest.main()
