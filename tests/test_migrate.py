import contextlib
import csv
import io
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from ledger import calc, cli, migrate
from ledger.journal import FIELDS_V1, load, v1_share_column
from ledger.migrate import MigrationError
from ledger.settings import load_settings
from tests.helpers import make_data

FIXTURE = Path(__file__).parent / "fixtures" / "journal_v1.csv"
# Комментарии и пустые строки должны пережить смену номера схемы байт в байт.
SETTINGS_V1 = (
    "# Настройки Ани: схему меняет только migrate\n"
    "schema = 1  # номер схемы журнала\n"
    'code = "."\n'
    "\n"
    "[owner]\n"
    'currency = "KZT"\n'
    'country = "KZ"\n'
    "\n"
    "[people.anya]\n"
    'name = "Аня"\n'
    'role = "партнёр"\n'
    "settlements = true\n"
    "default_share = 50\n"
    "\n"
    "[people.boris]\n"
    'name = "Борис"\n'
    'role = "работник"\n'
    "settlements = false\n"
)
SETTINGS_V2 = SETTINGS_V1.replace("schema = 1  #", "schema = 2  #")
MONTHS = ["2026-08", "2026-09", "2026-10"]


def raw_rows(path: Path) -> list[dict[str, str]]:
    """Сырые строки схемы 1 в том виде, в каком их видят формулы _v1_*."""
    return migrate._v1_rows(path.read_text(encoding="utf-8"), path.name)


def nonzero(totals: dict) -> dict:
    return {key: value for key, value in totals.items() if value != 0}


class MigrateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = make_data(Path(tmp.name) / "Мои деньги", SETTINGS_V1)
        self.original = FIXTURE.read_bytes()
        self.data.journal.write_bytes(self.original)
        self.settings_bytes = self.data.settings_file.read_bytes()
        self.backup = self.data.root / "journal.v1.csv"

    def assert_untouched(self):
        self.assertEqual(self.data.journal.read_bytes(), self.original)
        self.assertEqual(self.data.settings_file.read_bytes(), self.settings_bytes)
        self.assertFalse(self.backup.exists())
        self.assertEqual(sorted(p.name for p in self.data.root.glob("*.tmp")), [])

    def test_fixture_header_is_schema_1(self):
        with FIXTURE.open(encoding="utf-8", newline="") as fh:
            header = next(csv.reader(fh))
        self.assertEqual(header[10], "anya_share")
        self.assertEqual(v1_share_column(header), "anya_share")
        self.assertEqual(header[:10] + header[11:], list(FIELDS_V1[:10] + FIELDS_V1[11:]))
        types = {row["type"] for row in raw_rows(FIXTURE)}
        self.assertEqual(len(types), 8)

    def test_fixture_migrates(self):
        report = migrate.migrate(self.data, "anya")
        self.assertEqual(report.rows, 35)
        self.assertEqual(report.months, MONTHS)
        self.assertEqual(report.backup, self.backup)
        self.assertEqual(self.backup.read_bytes(), self.original)
        self.assertEqual(self.data.settings_file.read_text(encoding="utf-8"), SETTINGS_V2)

        settings = load_settings(self.data.settings_file)
        self.assertEqual(settings.schema, 2)
        entries = load(self.data.journal, settings)
        self.assertEqual(len(entries), 35)

        rows = raw_rows(FIXTURE)
        new_debts = {
            currency: amount
            for (person, currency), amount in calc.debts(entries).items()
            if person == "anya"
        }
        self.assertEqual(nonzero(new_debts), nonzero(migrate._v1_debts(rows)))
        # Подсчитано вручную по фикстуре: плюс — Аня должна мне.
        self.assertEqual(
            nonzero(calc.debts(entries)),
            {
                ("anya", "KZT"): Decimal("16700.25"),
                ("anya", "USD"): Decimal("10.5"),
                ("anya", "USDT"): Decimal("-12.5"),
                ("anya", "RUB"): Decimal("1000.5"),
            },
        )
        self.assertEqual(
            nonzero(calc.balances_by_currency(entries)), nonzero(migrate._v1_balances(rows))
        )

    def test_rows_are_translated(self):
        migrate.migrate(self.data, "anya")
        entries = {e.id: e for e in load(self.data.journal, load_settings(self.data.settings_file))}
        self.assertEqual(
            (entries[5].type, entries[5].party, entries[5].paid_by), ("debt", "anya", "me")
        )
        self.assertEqual((entries[6].type, entries[6].paid_by), ("debt", "anya"))
        self.assertEqual(entries[7].shares, {})
        self.assertEqual(entries[8].shares, {"anya": 50})
        self.assertEqual((entries[10].paid_by, entries[10].shares), ("anya", {"anya": 50}))
        self.assertEqual(entries[18].shares, {"anya": 100})
        self.assertEqual(entries[15].party, "anya")
        self.assertEqual(entries[22].party, "anya")
        self.assertEqual(entries[30].party, "anya")
        self.assertEqual(entries[7].party, "")
        self.assertEqual(entries[28].source_ref, "02.10.26 -5 100,00 ₸ Покупка Магазин Ромашка")
        self.assertEqual(entries[35].note, "билет Ани, за двоих")
        for e in entries.values():
            self.assertEqual(
                (e.book, e.account, e.account2, e.tax, e.receipt), ("", "", "", "", "")
            )

    def test_payer_of_own_money_types_becomes_me(self):
        # Схема 1 принимала paid_by человека у обмена, снятия и стартового остатка, а формулы
        # его не замечали: такие записи переходят на me, цифры прежние.
        self.original = self.original.replace(
            "обмен у Сокола,me,0".encode(), "обмен у Сокола,anya,0".encode(), 1
        ).replace("в банкомате Альфа,me,0".encode(), "в банкомате Альфа,anya,0".encode(), 1)
        self.data.journal.write_bytes(self.original)
        report = migrate.migrate(self.data, "anya")
        self.assertEqual(report.payer_reset, [12, 13])
        entries = {e.id: e for e in load(self.data.journal, load_settings(self.data.settings_file))}
        self.assertEqual((entries[12].paid_by, entries[13].paid_by), ("me", "me"))

    def test_second_migrate_refuses(self):
        migrate.migrate(self.data, "anya")
        journal = self.data.journal.read_bytes()
        with self.assertRaisesRegex(MigrationError, "уже схема 2"):
            migrate.migrate(self.data, "anya")
        self.assertEqual(self.data.journal.read_bytes(), journal)

    def test_person_without_settlements_refuses(self):
        with self.assertRaisesRegex(MigrationError, "boris"):
            migrate.migrate(self.data, "boris")
        self.assert_untouched()

    def test_unknown_person_refuses(self):
        with self.assertRaisesRegex(MigrationError, "vera"):
            migrate.migrate(self.data, "vera")
        self.assert_untouched()

    def test_other_share_column_name_migrates(self):
        # Колонка доли называется по-другому, а ключ в shares берётся из --person.
        self.original = self.original.replace(b",anya_share,", b",boris_share,", 1)
        self.data.journal.write_bytes(self.original)
        report = migrate.migrate(self.data, "anya")
        self.assertEqual(report.rows, 35)
        self.assertEqual(self.backup.read_bytes(), self.original)
        entries = {e.id: e for e in load(self.data.journal, load_settings(self.data.settings_file))}
        self.assertEqual(entries[8].shares, {"anya": 50})
        self.assertEqual(entries[18].shares, {"anya": 100})

    def test_share_column_off_pattern_refuses(self):
        for column in (b"share", b"Anya_share", b"anya_shares"):
            self.original = FIXTURE.read_bytes().replace(b",anya_share,", b"," + column + b",", 1)
            self.data.journal.write_bytes(self.original)
            with (
                self.subTest(column=column),
                self.assertRaisesRegex(MigrationError, "неожиданный заголовок"),
            ):
                migrate.migrate(self.data, "anya")
            self.assert_untouched()

    def test_header_not_schema_1_refuses(self):
        self.original = b"id,date\n1,2026-10-01\n"
        self.data.journal.write_bytes(self.original)
        with self.assertRaisesRegex(MigrationError, "заголовок"):
            migrate.migrate(self.data, "anya")
        self.assert_untouched()

    def test_leftover_backup_refuses(self):
        self.backup.write_bytes(b"old")
        with self.assertRaisesRegex(MigrationError, "journal.v1.csv"):
            migrate.migrate(self.data, "anya")
        self.assertEqual(self.backup.read_bytes(), b"old")
        self.assertEqual(self.data.journal.read_bytes(), self.original)
        self.assertEqual(self.data.settings_file.read_bytes(), self.settings_bytes)

    def test_two_different_payers_refuse(self):
        self.original = self.original.replace(b",anya,50,text", b",boris,50,text", 1)
        self.data.journal.write_bytes(self.original)
        with self.assertRaisesRegex(MigrationError, "boris"):
            migrate.migrate(self.data, "anya")
        self.assert_untouched()

    def test_broken_formula_rolls_back(self):
        with (
            mock.patch("ledger.calc.my_amount", side_effect=lambda e: e.amount),
            self.assertRaises(MigrationError) as caught,
        ):
            migrate.migrate(self.data, "anya")
        message = str(caught.exception)
        self.assertIn("не сошлось: categories", message)
        self.assertIn("2026-08 expense KZT кафе и доставка: 6000.0 ≠ 12000", message)
        self.assert_untouched()
        # Откат оставил всё как было: повтор проходит.
        self.assertEqual(migrate.migrate(self.data, "anya").rows, 35)

    RETURNED = "миграция отменена, журнал и настройки возвращены: не сошлось: "

    def test_balance_mismatch_rolls_back_and_says_so(self):
        real = calc.balances_by_currency

        def off(entries):
            totals = real(entries)
            totals["KZT"] += 1
            return totals

        with (
            mock.patch("ledger.calc.balances_by_currency", side_effect=off),
            self.assertRaises(MigrationError) as caught,
        ):
            migrate.migrate(self.data, "anya")
        self.assertTrue(str(caught.exception).startswith(self.RETURNED + "balance KZT"))
        self.assert_untouched()

    def test_debt_mismatch_rolls_back_and_says_so(self):
        real = calc.debts

        def off(entries):
            totals = real(entries)
            totals[("anya", "USD")] += 1
            return totals

        with (
            mock.patch("ledger.calc.debts", side_effect=off),
            self.assertRaises(MigrationError) as caught,
        ):
            migrate.migrate(self.data, "anya")
        self.assertTrue(str(caught.exception).startswith(self.RETURNED + "debt USD"))
        self.assert_untouched()

    def test_failed_journal_rollback_keeps_the_reason_and_says_where_the_journal_is(self):
        with (
            mock.patch("ledger.calc.my_amount", side_effect=lambda e: e.amount),
            mock.patch("ledger.migrate.replace_retrying", side_effect=PermissionError("занят")),
            self.assertRaises(MigrationError) as caught,
        ):
            migrate.migrate(self.data, "anya")
        message = str(caught.exception)
        self.assertIn("не сошлось: categories", message)
        self.assertIn("журнал в journal.v1.csv, поставьте schema = 1", message)
        self.assertEqual(self.backup.read_bytes(), self.original)

    def test_failed_settings_rollback_says_to_set_schema_1(self):
        real = migrate._write_text
        original = self.settings_bytes.decode("utf-8")  # на Windows с \r\n

        def write(path, text):
            if path == self.data.settings_file and text == original:
                raise PermissionError("занят")
            real(path, text)

        with (
            mock.patch("ledger.calc.my_amount", side_effect=lambda e: e.amount),
            mock.patch("ledger.migrate._write_text", side_effect=write),
            self.assertRaises(MigrationError) as caught,
        ):
            migrate.migrate(self.data, "anya")
        message = str(caught.exception)
        self.assertIn("не сошлось: categories", message)
        self.assertIn("журнал возвращён, поставьте schema = 1", message)
        self.assertEqual(self.data.journal.read_bytes(), self.original)

    def test_target_schema_is_2_whatever_the_code_knows(self):
        with mock.patch("ledger.migrate.SCHEMA", 3, create=True):
            migrate.migrate(self.data, "anya")
        self.assertEqual(self.data.settings_file.read_text(encoding="utf-8"), SETTINGS_V2)

    def test_v1_amounts_are_read_like_schema_1_did(self):
        self.assertEqual(migrate._v1_decimal(" 1 500,50 "), Decimal("1500.50"))
        for text in ("NaN", "Infinity", "1e3", "12abc"):
            with self.subTest(text=text), self.assertRaises(MigrationError):
                migrate._v1_decimal(text)

    def test_row_invalid_in_schema_2_rolls_back(self):
        self.original = self.original.replace(b",2200,KZT,", b",2200,EURO,")
        self.data.journal.write_bytes(self.original)
        with self.assertRaisesRegex(MigrationError, "запись #35"):
            migrate.migrate(self.data, "anya")
        self.assert_untouched()

    def test_failure_after_save_rolls_back(self):
        with (
            mock.patch("ledger.migrate._check", side_effect=RuntimeError("сбой")),
            self.assertRaises(RuntimeError),
        ):
            migrate.migrate(self.data, "anya")
        self.assert_untouched()


class MigrateCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = make_data(Path(tmp.name) / "Мои деньги", SETTINGS_V1)
        self.data.journal.write_bytes(FIXTURE.read_bytes())

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["--data", str(self.data.root), *argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_add_before_migrate_then_migrate(self):
        add = ("add", "--amount", "700", "--category", "прочее", "--desc", "Киоск")
        code, _, err = self.run_cli(*add)
        self.assertEqual(code, 1)
        self.assertIn("migrate", err)

        code, out, err = self.run_cli("migrate", "--person", "anya")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("записей 35", out)
        self.assertIn("journal.v1.csv", out)

        code, out, err = self.run_cli(*add)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("#36", out)

    def test_migrate_is_listed_in_help(self):
        code, out, _ = self.run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("migrate", out)

    def test_migrate_refusal_is_an_error_line(self):
        code, _, err = self.run_cli("migrate", "--person", "boris")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("Ошибка: "), err)


if __name__ == "__main__":
    unittest.main()
