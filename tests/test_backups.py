import contextlib
import io
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from ledger import backups, cli, ops
from ledger.journal import JournalError, LedgerError, load, locked
from tests.helpers import MINIMAL, make_data

NAME = re.compile(r"journal-\d{8}-\d{6}-\d{6}\.csv")


class BackupsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = make_data(Path(tmp.name) / "Мои деньги", MINIMAL)
        backups._kept_by_git.cache_clear()
        self.addCleanup(backups._kept_by_git.cache_clear)

    def add(self, amount: str):
        return ops.add_entry(
            self.data,
            date=date(2026, 10, 1),
            amount=amount,
            description="кофе",
            category="прочее",
            force=True,
        )

    def names(self) -> list[str]:
        return sorted(p.name for p in self.data.backups.glob("*"))

    def test_keeps_the_newest_30_copies_without_git(self):
        for i in range(32):
            self.add(str(i + 1))
        names = self.names()
        self.assertEqual(len(names), 30)
        self.assertTrue(all(NAME.fullmatch(name) for name in names), names)
        self.assertEqual(len(set(names)), 30)
        newest = self.data.backups / names[-1]
        # последняя копия снята перед 32-й записью: в ней 31 запись
        self.assertEqual(len(load(newest, self.data.settings)), 31)
        self.assertEqual([p.name for p in backups.listing(self.data)], names[::-1])

    def test_unchanged_journal_is_not_copied_twice(self):
        self.add("5")  # копия пустого журнала
        for _ in range(3):  # дубль без --force ничего не пишет
            result = ops.add_entry(self.data, date=date(2026, 10, 1), amount="5", category="прочее")
            self.assertIsNone(result.entry)
        self.assertEqual(len(self.names()), 2)

    def git(self, *args, cwd):
        subprocess.run(
            ["git", "-c", "user.name=Аня", "-c", "user.email=anya@example.invalid", *args],
            cwd=cwd,
            capture_output=True,
            check=True,
        )

    @unittest.skipUnless(shutil.which("git"), "нужен git")
    def test_no_copies_when_git_keeps_the_journal(self):
        self.git("init", "-q", cwd=self.data.root)
        self.git("add", "journal.csv", cwd=self.data.root)
        self.add("1")
        self.add("2")
        self.assertEqual(self.names(), [])
        self.assertIsNone(backups.before_write(self.data))

    @unittest.skipUnless(shutil.which("git"), "нужен git")
    def test_repository_above_that_does_not_keep_the_journal_does_not_count(self):
        home = self.data.root.parent  # как dotfiles-репозиторий в домашней папке
        self.git("init", "-q", cwd=home)
        (home / ".profile").write_text("x\n", encoding="utf-8")
        self.git("add", ".profile", cwd=home)
        self.git("commit", "-q", "-m", "dotfiles", cwd=home)
        self.assertFalse(backups.in_git(self.data.root))
        self.add("1")
        self.assertEqual(len(self.names()), 1)
        self.git("add", "Мои деньги/journal.csv", cwd=home)
        self.assertTrue(backups.in_git(self.data.root))

    def test_git_is_asked_once_per_folder_however_many_writes(self):
        # git выше по дереву: без кеша его спрашивали бы на каждой записи
        (self.data.root / ".git").mkdir()
        not_tracked = subprocess.CompletedProcess([], 1)
        with mock.patch("ledger.backups.subprocess.run", return_value=not_tracked) as run:
            self.add("1")
            self.add("2")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(self.names()), 2)

    def test_folder_named_git_or_missing_git_is_not_a_repository(self):
        (self.data.root / ".git").mkdir()  # не репозиторий
        self.assertFalse(backups.in_git(self.data.root))
        with mock.patch("ledger.backups.subprocess.run", side_effect=FileNotFoundError):
            self.assertFalse(backups.in_git(self.data.root))

    def test_no_copy_without_a_journal(self):
        self.data.journal.unlink()
        self.assertIsNone(backups.before_write(self.data))
        self.assertEqual(self.names(), [])

    def test_names_stay_unique_and_sorted_in_the_same_microsecond(self):
        moment = datetime(2026, 10, 23, 12, 0, 0, 999999)
        made = []
        with mock.patch("ledger.backups._now", return_value=moment):
            for i in range(3):
                self.data.journal.write_text(f"журнал {i}\n", encoding="utf-8")
                made.append(backups.before_write(self.data).name)
        self.assertEqual(len(set(made)), 3)
        self.assertEqual(made, sorted(made))
        self.assertEqual(made[0], "journal-20261023-120000-999999.csv")
        self.assertTrue(all(NAME.fullmatch(name) for name in made))

    def test_stray_file_with_a_date_like_name_does_not_break_writes(self):
        self.data.backups.mkdir()
        (self.data.backups / "journal-20261399-000000-000000.csv").write_text("", encoding="utf-8")
        self.add("1")
        self.assertEqual(len(self.names()), 2)

    def test_restore_returns_the_journal_from_a_copy(self):
        self.add("1")
        self.add("2")  # перед этой записью снята копия с одной записью
        newest = backups.listing(self.data)[0]
        current = backups.restore(self.data, newest.name)
        amounts = [str(e.amount) for e in load(self.data.journal, self.data.settings)]
        self.assertEqual(amounts, ["1"])
        # текущий журнал перед возвратом тоже скопирован
        self.assertEqual(len(load(current, self.data.settings)), 2)
        self.assertEqual(current.parent, self.data.backups)

    def test_restore_of_the_oldest_copy_survives_the_pruning(self):
        for i in range(31):
            self.add(str(i + 1))
        oldest = backups.listing(self.data)[-1]
        expected = oldest.read_bytes()
        backups.restore(self.data, oldest.name)
        self.assertEqual(self.data.journal.read_bytes(), expected)
        self.assertEqual(len(backups.listing(self.data)), 30)

    def test_restore_of_a_broken_copy_is_refused_and_the_journal_stays(self):
        self.add("1")
        self.add("2")
        broken = backups.listing(self.data)[0]
        broken.write_text("id,date\n1,вчера\n", encoding="utf-8")
        before = self.data.journal.read_bytes()
        count = len(backups.listing(self.data))
        with self.assertRaises(LedgerError):
            backups.restore(self.data, broken.name)
        self.assertEqual(self.data.journal.read_bytes(), before)
        self.assertEqual(len(backups.listing(self.data)), count)

    def test_restore_accepts_only_a_listed_name(self):
        self.add("1")
        self.add("2")
        before = self.data.journal.read_bytes()
        for name in ("../settings.toml", "journal.csv", "нет-такой.csv", ""):
            with self.subTest(name=name), self.assertRaises(LedgerError):
                backups.restore(self.data, name)
        self.assertEqual(self.data.journal.read_bytes(), before)

    def test_restore_refuses_inside_another_write(self):
        self.add("1")
        self.add("2")
        name = backups.listing(self.data)[0].name
        with locked(self.data.journal), self.assertRaises(JournalError):
            backups.restore(self.data, name)


class RestoreCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = make_data(Path(tmp.name) / "Мои деньги", MINIMAL)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--data", str(self.data.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_restore_without_a_name_lists_copies(self):
        code, out, _ = self.run_cli("restore")
        self.assertEqual(code, 0)
        self.assertIn("Копий журнала нет", out)
        for amount in ("1", "2", "3"):
            self.run_cli("add", "--amount", amount, "--category", "прочее", "--force")
        code, out, _ = self.run_cli("restore")
        self.assertEqual(code, 0)
        names = [p.name for p in backups.listing(self.data)]
        self.assertEqual(len(names), 3)
        for name in names:
            self.assertIn(name, out)
        self.assertLess(out.index(names[0]), out.index(names[-1]))  # новые сверху

    def test_restore_with_a_name_returns_the_journal(self):
        for amount in ("1", "2"):
            self.run_cli("add", "--amount", amount, "--category", "прочее", "--force")
        name = backups.listing(self.data)[0].name
        code, out, err = self.run_cli("restore", name)
        self.assertEqual(code, 0, err)
        self.assertIn(name, out)
        self.assertEqual(len(load(self.data.journal, self.data.settings)), 1)

    def test_restore_of_an_unknown_name_is_an_error(self):
        code, _, err = self.run_cli("restore", "journal-20000101-000000-000000.csv")
        self.assertEqual(code, 1)
        self.assertIn("Ошибка:", err)


if __name__ == "__main__":
    unittest.main()
