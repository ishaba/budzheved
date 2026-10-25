import contextlib
import csv
import io
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from ledger import cli, export
from ledger.journal import FIELDS, Entry, LedgerError, load
from tests.helpers import MINIMAL, make_data, make_settings

ENTRIES = [
    Entry(
        id=1,
        date=date(2026, 10, 1),
        type="expense",
        amount=Decimal("1500.50"),
        currency="KZT",
        category="кафе и доставка",
        description="кофе; с собой",
    ),
    Entry(
        id=2,
        date=date(2026, 10, 2),
        type="exchange",
        amount=Decimal("100"),
        currency="USDT",
        amount2=Decimal("45000.25"),
        currency2="KZT",
        note="обмен 1.5",
    ),
]


class ExcelTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "выгрузка.csv"

    def test_semicolons_decimal_comma_and_bom(self):
        export.excel(ENTRIES, make_settings(MINIMAL), self.path)
        raw = self.path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8-sig")
        self.assertIn(";1500,50;", text)
        self.assertIn(";45000,25;", text)
        self.assertIn("обмен 1.5", text)  # точка в тексте остаётся точкой
        with self.path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh, delimiter=";"))
        self.assertEqual(rows[0], list(FIELDS))
        self.assertEqual([row[0] for row in rows[1:]], ["1", "2"])
        self.assertEqual(rows[1][FIELDS.index("description")], "кофе; с собой")
        self.assertEqual(rows[1][FIELDS.index("amount2")], "")

    def test_overwrites_and_leaves_no_temporary_files(self):
        self.path.write_text("старое", encoding="utf-8")
        export.excel(ENTRIES[:1], make_settings(MINIMAL), self.path)
        self.assertNotIn("старое", self.path.read_text(encoding="utf-8-sig"))
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [self.path.name])

    def test_text_that_excel_would_run_as_a_formula_is_escaped(self):
        refund = Entry(
            id=3,
            date=date(2026, 10, 3),
            type="expense",
            amount=Decimal("-5.00"),
            currency="KZT",
            category="прочее",
            description='=HYPERLINK("x")',
            note="+7 700",
            source_ref="@сумма",
        )
        tricky = ["-минус", "\tтаб", "\rвозврат"]
        export.excel([refund], make_settings(MINIMAL), self.path)
        with self.path.open(encoding="utf-8-sig", newline="") as fh:
            row = list(csv.reader(fh, delimiter=";"))[1]
        self.assertEqual(row[FIELDS.index("description")], '\'=HYPERLINK("x")')
        self.assertEqual(row[FIELDS.index("note")], "'+7 700")
        self.assertEqual(row[FIELDS.index("source_ref")], "'@сумма")
        self.assertEqual(row[FIELDS.index("amount")], "-5,00")
        self.assertEqual(row[FIELDS.index("category")], "прочее")
        for text in tricky:
            self.assertEqual(export.cell(text), "'" + text)
        self.assertEqual(export.cell("кофе = 5"), "кофе = 5")

    def test_unwritable_target_is_a_ledger_error(self):
        self.path.mkdir()  # на месте файла папка: как файл, который держит Excel
        with mock.patch("ledger.journal.time.sleep"), self.assertRaises(LedgerError) as caught:
            export.excel(ENTRIES, make_settings(MINIMAL), self.path)
        self.assertIn("Excel", str(caught.exception))


class ExportCliTest(unittest.TestCase):
    def test_export_excel_writes_every_entry(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = make_data(Path(tmp.name) / "Мои деньги", MINIMAL)
        target = Path(tmp.name) / "таблица.csv"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            for amount in ("1500.50", "7"):
                cli.main(["--data", str(data.root), "add", "--amount", amount, "--force"])
            code = cli.main(["--data", str(data.root), "export", "--excel", str(target)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("записей 2", out.getvalue())
        with target.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh, delimiter=";"))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][FIELDS.index("amount")], "1500,50")
        self.assertEqual(len(load(data.journal, data.settings)), 2)

    def test_export_refuses_to_overwrite_the_data_files(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = make_data(Path(tmp.name) / "Мои деньги", MINIMAL)
        data.merchants.write_text("pattern,category\n", encoding="utf-8")
        data.backups.mkdir()
        (data.backups / "journal-20261001-000000-000000.csv").write_bytes(data.journal.read_bytes())
        files = [data.journal, data.settings_file, data.merchants]
        before = {f: f.read_bytes() for f in [*files, *data.backups.iterdir()]}
        targets = [
            *files,
            data.root / "backups" / "journal-20261001-000000-000000.csv",
            data.root / "backups" / "new.csv",
            data.root / "receipts" / ".." / "journal.csv",
        ]
        alias = Path(tmp.name) / "ярлык.csv"
        try:
            alias.symlink_to(data.journal)
        except OSError:  # на Windows без прав символьные ссылки недоступны
            pass
        else:
            targets.append(alias)
        for target in targets:
            with self.subTest(target=target):
                err = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    code = cli.main(["--data", str(data.root), "export", "--excel", str(target)])
                self.assertEqual(code, 1)
                self.assertIn("выгрузка не пишется поверх файлов папки данных", err.getvalue())
        self.assertEqual({f: f.read_bytes() for f in before}, before)
        self.assertFalse((data.backups / "new.csv").exists())


if __name__ == "__main__":
    unittest.main()
