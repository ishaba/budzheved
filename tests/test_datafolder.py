import ast
import contextlib
import io
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ledger import cli, journal
from ledger.datafolder import LAUNCHER, init_folder
from ledger.extensions import CODE_ROOT
from ledger.journal import LedgerError
from ledger.settings import load_settings


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def run_launcher(folder: Path, *argv):
    return subprocess.run(
        [sys.executable, str(folder / "budget.py"), *argv],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
    )


class InitTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "Мои деньги"

    def test_init_creates_every_file_without_settings(self):
        code, out, err = run_cli("--data", str(self.folder), "init")
        self.assertEqual(code, 0, err)
        self.assertIn("Папка данных готова", out)
        s = load_settings(self.folder / "settings.toml")
        self.assertEqual((s.schema, s.code), (2, CODE_ROOT.as_posix()))
        self.assertEqual((s.currency, s.country), ("USDT", "XX"))
        with (self.folder / "journal.csv").open(encoding="utf-8") as fh:
            self.assertEqual(fh.readline().strip(), ",".join(journal.FIELDS))
        merchants = (self.folder / "merchants.csv").read_text(encoding="utf-8")
        self.assertEqual(merchants.strip(), "pattern,category")
        for name in ("receipts", "extensions", "backups"):
            self.assertTrue((self.folder / name).is_dir(), name)
        launcher = (self.folder / "budget.py").read_bytes()
        self.assertEqual(launcher, LAUNCHER.read_bytes())
        ignored = (self.folder / ".gitignore").read_text(encoding="utf-8").split()
        self.assertEqual(ignored, ["*.lock", "*.tmp", "__pycache__/", "backups/"])
        agents = (self.folder / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(agents.startswith("<!-- budzheved:begin -->"))
        self.assertIn("## Моё", agents)
        self.assertEqual((self.folder / "CLAUDE.md").read_text(encoding="utf-8"), "@AGENTS.md\n")

    def test_init_takes_currency_and_country(self):
        code, _, err = run_cli(
            "--data", str(self.folder), "init", "--currency", "KZT", "--country", "KZ"
        )
        self.assertEqual(code, 0, err)
        s = load_settings(self.folder / "settings.toml")
        self.assertEqual((s.currency, s.country), ("KZT", "KZ"))

    def test_init_normalises_the_country(self):
        code, _, err = run_cli("--data", str(self.folder), "init", "--country", " kz ")
        self.assertEqual(code, 0, err)
        self.assertEqual(load_settings(self.folder / "settings.toml").country, "KZ")

    def test_init_rejects_a_bad_currency(self):
        code, _, err = run_cli("--data", str(self.folder), "init", "--currency", "rub")
        self.assertEqual(code, 1)
        self.assertIn("валюта", err)
        self.assertFalse((self.folder / "settings.toml").exists())

    def test_second_init_refuses(self):
        init_folder(self.folder, CODE_ROOT, "USDT", "XX")
        with self.assertRaisesRegex(LedgerError, "папка уже настроена"):
            init_folder(self.folder, CODE_ROOT, "USDT", "XX")
        code, _, err = run_cli("--data", str(self.folder), "init")
        self.assertEqual(code, 1)
        self.assertIn("папка уже настроена", err)

    def test_init_keeps_files_already_in_the_folder(self):
        self.folder.mkdir(parents=True)
        (self.folder / "journal.csv").write_text("старый журнал\n", encoding="utf-8")
        init_folder(self.folder, CODE_ROOT, "USDT", "XX")
        text = (self.folder / "journal.csv").read_text(encoding="utf-8")
        self.assertEqual(text, "старый журнал\n")


class LauncherTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "Мои деньги"
        init_folder(self.folder, CODE_ROOT, "USDT", "XX")

    def test_launcher_runs_the_core_with_its_folder(self):
        added = run_launcher(self.folder, "add", "--amount", "5", "--desc", "чай")
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIn("чай", added.stdout)
        balance = run_launcher(self.folder, "report", "balance")
        self.assertEqual(balance.returncode, 0, balance.stderr)
        self.assertIn("USDT: -5,00", balance.stdout)

    def test_launcher_names_a_missing_code_path(self):
        missing = (self.folder / "нет такого кода").as_posix()
        settings = self.folder / "settings.toml"
        text = settings.read_text(encoding="utf-8")
        settings.write_text(
            text.replace(f'code = "{CODE_ROOT.as_posix()}"', f'code = "{missing}"'),
            encoding="utf-8",
        )
        done = run_launcher(self.folder, "report", "balance")
        self.assertEqual(done.returncode, 1)
        self.assertIn("Код Бюджеведа не найден по пути из settings.toml", done.stderr)
        self.assertIn(missing, done.stderr)

    def test_launcher_parses_as_python_3_6(self):
        source = LAUNCHER.read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 6))


class CodeEntryTest(unittest.TestCase):
    """budget.py папки кода: им README зовёт init, и Python бывает старым."""

    ENTRY = CODE_ROOT / "budget.py"

    def test_old_python_gets_the_launcher_message(self):
        err = io.StringIO()
        with (
            mock.patch.object(sys, "version_info", (3, 10, 12, "final", 0)),
            mock.patch.object(cli, "main") as core,
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as caught,
        ):
            runpy.run_path(str(self.ENTRY), run_name="__main__")
        self.assertEqual(caught.exception.code, 1)
        self.assertIn(launcher_message(), err.getvalue())
        core.assert_not_called()

    def test_entry_and_launcher_say_the_same(self):
        entry = runpy.run_path(str(self.ENTRY), run_name="budget")
        self.assertEqual(entry["OLD_PYTHON"], launcher_message())

    def test_entry_parses_as_python_3_6(self):
        ast.parse(self.ENTRY.read_text(encoding="utf-8"), feature_version=(3, 6))


def launcher_message() -> str:
    tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.targets[0].id == "OLD_PYTHON":
            return node.value.value
    raise AssertionError("в launcher.py нет OLD_PYTHON")


if __name__ == "__main__":
    unittest.main()
