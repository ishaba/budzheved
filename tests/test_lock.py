import errno
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from ledger import kaspi, lock, ops
from ledger.kaspi import KaspiError
from ledger.lock import exclusive
from tests.helpers import make_data

ROOT = Path(__file__).resolve().parent.parent

HOLDER = """
import sys, time
from pathlib import Path
from ledger.lock import exclusive

with exclusive(Path(sys.argv[1])):
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def start_holder(path, seconds):
    """Дочерний процесс берёт замок и пишет «held» в stdout, когда взял."""
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(path), str(seconds)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "held"
    return proc


class LockTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def spawn(self, path, seconds):
        proc = start_holder(path, seconds)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def test_second_holder_waits(self):
        path = self.tmp / "journal.csv.lock"
        self.spawn(path, 1)
        started = time.monotonic()
        with exclusive(path):
            waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.8)

    def test_killed_holder_releases(self):
        path = self.tmp / "journal.csv.lock"
        proc = self.spawn(path, 60)
        proc.kill()
        proc.wait()
        started = time.monotonic()
        with exclusive(path):
            waited = time.monotonic() - started
        self.assertLess(waited, 2)

    def test_cyrillic_path(self):
        with exclusive(self.tmp / "Мои деньги" / "journal.csv.lock"):
            pass

    def test_reusable_after_release(self):
        path = self.tmp / "journal.csv.lock"
        for _ in range(3):
            with exclusive(path):
                pass


class StdoutUtf8Test(unittest.TestCase):
    def test_stdout_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = make_data(Path(tmp) / "Мои деньги")
            ops.add_entry(
                data,
                date=date(2026, 10, 1),
                amount="500",
                description="ёжик №5 — тест ✓",
                category="прочее",
                force=True,
            )
            env = {**os.environ, "PYTHONIOENCODING": "cp1251"}
            proc = subprocess.run(
                [sys.executable, "budget.py", "--data", str(data.root), "list"],
                cwd=ROOT,
                env=env,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("ёжик №5 — тест ✓", proc.stdout.decode("utf-8"))


class PdftotextMissingTest(unittest.TestCase):
    def test_pdftotext_missing(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(KaspiError, "Poppler"):
                kaspi.extract_text(Path("x.pdf"))


class WindowsRetryTest(unittest.TestCase):
    """Ожидание замка на Windows: повтор только при занятом замке, остальное — ошибка."""

    def test_retries_only_while_the_lock_is_taken(self):
        busy = [
            OSError(errno.EDEADLK, "deadlock"),
            OSError(errno.EACCES, "taken"),
            PermissionError(errno.EACCES, "taken"),
        ]
        winerror = OSError(0, "locked")
        winerror.winerror = 33
        attempts = iter([*busy, winerror, None])

        def lock_once():
            failure = next(attempts)
            if failure is not None:
                raise failure

        lock.retry_while_taken(lock_once)
        self.assertIsNone(next(attempts, None))

    def test_other_errors_are_raised_at_once(self):
        calls = []

        def lock_once():
            calls.append(1)
            raise OSError(errno.EBADF, "bad file descriptor")

        with self.assertRaises(OSError):
            lock.retry_while_taken(lock_once)
        self.assertEqual(len(calls), 1)
