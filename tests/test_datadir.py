import contextlib
import io
import unittest
from pathlib import Path

from ledger import cli
from ledger.datadir import DataDir


class DataDirTest(unittest.TestCase):
    def test_paths_live_in_root(self):
        d = DataDir(Path("/x/Мои деньги"))
        self.assertEqual(d.journal, Path("/x/Мои деньги/journal.csv"))
        self.assertEqual(d.backups, Path("/x/Мои деньги/backups"))

    def test_other_paths(self):
        d = DataDir(Path("/x"))
        self.assertEqual(d.merchants, Path("/x/merchants.csv"))
        self.assertEqual(d.settings_file, Path("/x/settings.toml"))
        self.assertEqual(d.receipts, Path("/x/receipts"))
        self.assertEqual(d.extensions, Path("/x/extensions"))

    def test_is_frozen(self):
        d = DataDir(Path("/x"))
        with self.assertRaises(AttributeError):
            d.root = Path("/y")  # type: ignore[misc]

    def test_cli_requires_data(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["report", "balance"])

    def test_cli_resolves_data_path(self):
        parsed = cli.build_parser(None).parse_args(["--data", "~/Мои деньги/../x", "check"])
        self.assertEqual(parsed.data, DataDir(Path("~/Мои деньги/../x").expanduser().resolve()))


if __name__ == "__main__":
    unittest.main()
