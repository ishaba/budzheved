import contextlib
import io
import tempfile
import textwrap
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from ledger import cli, extensions, ops
from ledger.extensions import ExtensionError, guarded, load_extensions
from ledger.journal import JournalError, LedgerError
from ledger.journal import load as load_journal
from ledger.tax import parse_period
from tests.helpers import make_data

FIXTURE = Path(__file__).parent / "fixtures" / "kaspi_sample.txt"

TOML = """\
schema = 2
code = "."

[owner]
currency = "KZT"
country = "KZ"

[books]
default = "личное"
names = ["личное", "бизнес"]

[accounts.card]
book = "личное"
currencies = ["KZT"]
import = "kaspi"

[accounts.shop]
book = "бизнес"
currencies = ["KZT"]
import = "demo"

[people.buyer]
name = "Покупатель"
role = "покупатель"

[tax."бизнес"]
rate = 3
base = "доходы"
period = "полугодие"
categories = ["продажи"]

[categories]
income = ["продажи"]
"""

DEMO = """\
def parse(path):
    return {
        "entries": [
            {"date": "2026-10-01", "type": "expense", "amount": "1000", "currency": "KZT",
             "category": "продукты", "description": "магазин", "source_ref": "r1"},
            {"date": "2026-10-02", "type": "expense", "amount": "500", "currency": "KZT",
             "category": "CATEGORY", "description": "такси", "source_ref": "r2"},
        ],
        "totals": {"KZT": "TOTAL"},
    }


def register(api):
    api.importer("demo", parse)
"""


def demo(total="-1500.00", category="транспорт"):
    return DEMO.replace("TOTAL", total).replace("CATEGORY", category)


class ExtensionsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = make_data(Path(tmp.name) / "Мои деньги", TOML)
        self.data.extensions.mkdir()
        self.statement = Path(tmp.name) / "выписка.csv"
        self.statement.write_text("any", encoding="utf-8")

    def write(self, name, text):
        (self.data.extensions / name).write_text(textwrap.dedent(text), encoding="utf-8")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["--data", str(self.data.root), *argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def load(self):
        return load_journal(self.data.journal, self.data.settings)

    def import_demo(self, *argv):
        return self.run_cli("import", "demo", str(self.statement), *argv)

    # импорт

    def test_importer_writes_rows_with_its_source_and_account(self):
        self.write("demo.py", demo())
        code, out, err = self.import_demo()
        self.assertEqual((code, err), (0, ""))
        entries = self.load()
        self.assertEqual(len(entries), 2)
        self.assertEqual({e.source for e in entries}, {"import:demo"})
        self.assertEqual({e.account for e in entries}, {"shop"})
        self.assertEqual([e.source_ref for e in entries], ["r1", "r2"])
        self.assertIn("новых 2", out)

    def test_reimport_skips_rows_already_written(self):
        self.write("demo.py", demo())
        self.import_demo()
        before = self.data.journal.read_bytes()
        code, out, err = self.import_demo()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("новых 0", out)
        self.assertIn("Пропущено как уже записанное:", out)
        self.assertIn("#1 2026-10-01", out)
        self.assertIn("#2 2026-10-02", out)
        self.assertEqual(self.data.journal.read_bytes(), before)

    def test_dry_run_writes_nothing(self):
        self.write("demo.py", demo())
        code, out, _ = self.import_demo("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Пробный запуск", out)
        self.assertEqual(self.load(), [])

    def test_wrong_totals_write_nothing(self):
        self.write("demo.py", demo(total="-1400.00"))
        before = self.data.journal.read_bytes()
        code, out, _ = self.import_demo()
        self.assertEqual(code, 1)
        self.assertIn("ничего не записано", out)
        self.assertIn("KZT: по строкам -1500, в итогах -1400.00", out)
        self.assertEqual(self.data.journal.read_bytes(), before)

    def test_unknown_category_refuses_the_whole_import_with_row_number(self):
        self.write("demo.py", demo(category="нет такой"))
        before = self.data.journal.read_bytes()
        code, _, err = self.import_demo()
        self.assertEqual(code, 1)
        self.assertIn("строка 2: неизвестная категория расхода 'нет такой'", err)
        self.assertIn("ничего не записано", err)
        self.assertEqual(self.data.journal.read_bytes(), before)

    def test_import_rows_checks_required_fields_and_names(self):
        rows = [{"date": "2026-10-01", "type": "expense", "amount": "5", "currency": "KZT"}]
        with self.assertRaises(JournalError) as caught:
            ops.import_rows(self.data, "demo", rows, None, "shop")
        self.assertIn("строка 1: нет обязательного поля source_ref", str(caught.exception))
        rows[0].update(source_ref="x", colour="red")
        with self.assertRaises(JournalError) as caught:
            ops.import_rows(self.data, "demo", rows, None, "shop")
        self.assertIn("строка 1: неизвестное поле 'colour'", str(caught.exception))
        for name in ("Demo", "a.b", "a/b", ""):
            with self.subTest(name=name), self.assertRaises(JournalError):
                ops.import_rows(self.data, name, [], None)

    def test_import_rows_keeps_given_account_and_fills_empty_ones(self):
        rows = [
            {"date": "2026-10-01", "type": "expense", "amount": "5", "currency": "KZT",
             "category": "продукты", "source_ref": "a", "account": "card"},
            {"date": "2026-10-01", "type": "expense", "amount": "6", "currency": "KZT",
             "category": "продукты", "source_ref": "b"},
        ]  # fmt: skip
        result = ops.import_rows(self.data, "demo", rows, None, "shop")
        self.assertTrue(result.saved)
        self.assertEqual([e.account for e in self.load()], ["card", "shop"])

    def test_import_rows_match_duplicates_within_their_account(self):
        def row(account):
            return {"date": "2026-10-01", "type": "expense", "amount": "5", "currency": "KZT",
                    "category": "продукты", "source_ref": "same", "account": account}  # fmt: skip

        self.assertEqual(len(ops.import_rows(self.data, "demo", [row("card")], None).added), 1)
        other = ops.import_rows(self.data, "demo", [row("shop"), row("shop")], None)
        self.assertEqual((len(other.added), other.skipped), (2, []))
        again = ops.import_rows(self.data, "demo", [row("card"), row("shop"), row("shop")], None)
        self.assertEqual((len(again.added), len(again.skipped)), (0, 3))
        self.assertEqual(sorted(e.account for e in self.load()), ["card", "shop", "shop"])

    def test_unknown_importer(self):
        code, _, err = self.import_demo()
        self.assertEqual(code, 1)
        self.assertIn("импортёра «demo» нет", err)

    # загрузка и ошибки расширений

    def test_broken_extension_is_switched_off_alone(self):
        self.write("bad.py", "def register(api):\n    raise RuntimeError('сломалось')\n")
        self.write("demo.py", demo())
        loaded = load_extensions(self.data)
        self.assertEqual(len(loaded.errors), 1)
        self.assertIn("bad", loaded.errors[0])
        self.assertIn("сломалось", loaded.errors[0])
        self.assertEqual(list(loaded.importers), ["demo"])
        code, out, err = self.run_cli("report", "balance")
        self.assertEqual(code, 0)
        self.assertIn("Внимание: ", err)
        self.assertIn("bad", err)
        self.assertIn("Остатки", out)

    def test_syntax_error_and_missing_register(self):
        self.write("broken.py", "def register(api)\n")
        self.write("empty.py", "X = 1\n")
        errors = load_extensions(self.data).errors
        self.assertEqual(len(errors), 2)
        self.assertIn("broken", errors[0])
        self.assertIn("empty", errors[1])
        self.assertIn("register", errors[1])

    def test_test_files_are_not_loaded_as_extensions(self):
        self.write("test_demo.py", "raise RuntimeError('не грузить')\n")
        self.assertEqual(load_extensions(self.data).errors, [])

    def test_names_that_clash_switch_off_that_extension_only(self):
        self.write(
            "a_shadow.py",
            """\
            def register(api):
                api.importer("shadowed", lambda path: [])
                api.report("balance", lambda entries, s, args: "")
            """,
        )
        self.write("b_demo.py", demo())
        self.write("c_twin.py", demo())
        self.write("d_kaspi.py", "def register(api):\n    api.importer('kaspi', lambda p: [])\n")
        self.write("e_book.py", "def register(api):\n    api.tax_rule('нет', lambda e, s: 0)\n")
        loaded = load_extensions(self.data)
        self.assertEqual(list(loaded.importers), ["demo"])
        self.assertEqual(loaded.reports, {})
        self.assertEqual(len(loaded.errors), 4)
        self.assertIn("balance", loaded.errors[0])
        self.assertIn("demo", loaded.errors[1])
        self.assertIn("kaspi", loaded.errors[2])
        self.assertIn("нет", loaded.errors[3])

    def test_extension_that_writes_the_journal_is_stopped_and_rolled_back(self):
        self.write(
            "sneaky.py",
            """\
            from pathlib import Path

            DATA = None

            def parse(path):
                (DATA / "journal.csv").write_text("broken", encoding="utf-8")
                (DATA / "journal.seq").write_text("99", encoding="utf-8")
                return []

            def register(api):
                api.importer("sneaky", parse)
            """,
        )
        before = self.data.journal.read_bytes()
        loaded = load_extensions(self.data)
        module = loaded.importers["sneaky"].__globals__
        module["DATA"] = self.data.root
        with self.assertRaises(ExtensionError) as caught:
            guarded(self.data, "sneaky", loaded.importers["sneaky"], self.statement)
        self.assertIn("расширение sneaky пыталось изменить", str(caught.exception))
        self.assertIn("journal.csv", str(caught.exception))
        self.assertIn("мимо основы", str(caught.exception))
        self.assertEqual(self.data.journal.read_bytes(), before)
        self.assertFalse(self.data.journal.with_suffix(".seq").exists())

    def test_extension_that_rewrites_merchant_rules_is_rolled_back(self):
        self.data.merchants.write_text("pattern,category\nтакси,транспорт\n", encoding="utf-8")
        rules = self.data.merchants.read_bytes()

        def parse(path):
            self.data.merchants.write_text("pattern,category\nтакси,прочее\n", encoding="utf-8")
            return []

        with self.assertRaises(ExtensionError) as caught:
            guarded(self.data, "sneaky", parse, self.statement)
        self.assertIn("merchants.csv мимо основы", str(caught.exception))
        self.assertEqual(self.data.merchants.read_bytes(), rules)

    def test_restore_goes_on_past_a_file_it_cannot_write_and_names_it(self):
        self.data.merchants.write_text("pattern,category\nтакси,транспорт\n", encoding="utf-8")
        files = (self.data.journal, self.data.merchants)
        before = [f.read_bytes() for f in files]

        def parse(path):
            for f in files:
                f.write_text("испорчено", encoding="utf-8")
            return []

        real = extensions._write_bytes

        def write(path, content):
            if path == self.data.journal:
                raise PermissionError(13, "занят")
            real(path, content)

        with (
            mock.patch("ledger.extensions._write_bytes", side_effect=write),
            self.assertRaises(ExtensionError) as caught,
        ):
            guarded(self.data, "sneaky", parse, self.statement)
        message = str(caught.exception)
        self.assertIn("journal.csv, merchants.csv мимо основы", message)
        self.assertIn("вернуть не удалось: journal.csv (занят)", message)
        self.assertEqual(self.data.merchants.read_bytes(), before[1])
        self.assertEqual(list(self.data.root.glob("*.tmp")), [])

    def test_extension_that_writes_while_loading_is_rolled_back_and_switched_off(self):
        self.data.merchants.write_text("pattern,category\n", encoding="utf-8")
        self.write(
            "a_top.py",
            """\
            from pathlib import Path

            (Path(__file__).parent.parent / "journal.csv").write_text("x", encoding="utf-8")

            def register(api):
                api.report("top", lambda entries, s, args: "")
            """,
        )
        self.write(
            "b_register.py",
            """\
            from pathlib import Path

            def register(api):
                root = Path(__file__).parent.parent
                (root / "settings.toml").write_text("broken", encoding="utf-8")
                (root / "merchants.csv").write_text("x", encoding="utf-8")
                api.report("reg", lambda entries, s, args: "")
            """,
        )
        self.write("c_demo.py", demo())
        files = (self.data.journal, self.data.settings_file, self.data.merchants)
        before = [f.read_bytes() for f in files]
        loaded = load_extensions(self.data)
        self.assertEqual([f.read_bytes() for f in files], before)
        self.assertFalse(self.data.journal.with_suffix(".seq").exists())
        self.assertEqual(list(loaded.importers), ["demo"])
        self.assertEqual(loaded.reports, {})
        self.assertEqual(
            loaded.errors,
            [
                "расширение a_top.py пыталось изменить journal.csv мимо основы и выключено",
                "расширение b_register.py пыталось изменить settings.toml, merchants.csv "
                "мимо основы и выключено",
            ],
        )
        code, out, err = self.run_cli("report", "balance")
        self.assertEqual(code, 0)
        self.assertEqual(err.count("Внимание: "), 2)
        self.assertEqual([f.read_bytes() for f in files], before)

    def test_extension_that_writes_through_the_core_does_not_hang(self):
        def parse(path):
            ops.add_entry(self.data, date=date(2026, 10, 1), amount="5", currency="KZT")
            return []

        with self.assertRaises(ExtensionError) as caught:
            guarded(self.data, "pushy", parse, self.statement)
        self.assertIn("расширение pushy", str(caught.exception))
        self.assertEqual(self.load(), [])

    def test_exception_inside_extension(self):
        def parse(path):
            raise ValueError("плохой файл")

        with self.assertRaises(ExtensionError) as caught:
            guarded(self.data, "demo", parse, self.statement)
        self.assertEqual(str(caught.exception), "расширение demo: плохой файл")

    def test_importer_returning_garbage(self):
        self.write("demo.py", "def register(api):\n    api.importer('demo', lambda p: 42)\n")
        code, _, err = self.import_demo()
        self.assertEqual(code, 1)
        self.assertIn("расширение demo", err)

    # отчёты и налог

    def test_extension_report(self):
        self.write(
            "purchases.py",
            """\
            def report(entries, settings, args):
                assert isinstance(entries, tuple)
                entries[0].amount = 0  # копия: журнал это не меняет
                return f"закупок {len(entries)}, аргументы {args}"

            def register(api):
                api.report("закупки", report)
            """,
        )
        self.run_cli("add", "--amount", "10", "--date", "2026-10-01")
        code, out, err = self.run_cli("report", "закупки", "2026-10", "boris")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "закупок 1, аргументы ['2026-10', 'boris']\n")
        self.assertEqual(str(self.load()[0].amount), "10")

    def test_unknown_report_and_stray_arguments(self):
        code, _, err = self.run_cli("report", "закупки")
        self.assertEqual(code, 1)
        self.assertIn("неизвестный отчёт «закупки»", err)
        code, _, err = self.run_cli("report", "balance", "лишнее")
        self.assertEqual(code, 1)
        self.assertIn("лишнее", err)

    def write_tax_rule(self, book):
        self.write(
            "tax_rule.py",
            f"""\
            from decimal import Decimal
            from ledger.tax import TaxResult

            def rule(entries, settings):
                total = sum((e.amount for e in entries), Decimal(0))
                return TaxResult({{"KZT": total}}, {{"KZT": total / 10}}, [], "своё правило")

            def register(api):
                api.tax_rule("{book}", rule)
            """,
        )

    def test_extension_tax_rule_keeps_the_core_decisions(self):
        self.write_tax_rule("бизнес")
        for argv in (
            ("--account", "shop", "--category", "продажи", "--amount", "1000"),
            ("--account", "shop", "--category", "доход", "--tax", "да", "--amount", "100"),
            ("--account", "card", "--party", "buyer", "--amount", "50"),
        ):
            code, _, err = self.run_cli(
                "add", "--type", "income", "--date", "2026-10-01", "--force", *argv
            )
            self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("report", "tax", "--book", "бизнес", "--period", "2026-H2")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("своё правило", out)
        self.assertIn("KZT: база 1 100,00, налог 110,00", out)
        self.assertIn("#2 tax=да, но категория не облагается", out)
        self.assertIn("#3 доход от покупателя на счёте книги «личное»", out)

    def test_extension_tax_rule_for_a_book_without_tax_section(self):
        self.write_tax_rule("личное")
        self.run_cli("add", "--type", "income", "--account", "card", "--amount", "70",
                     "--date", "2026-10-01", "--tax", "нет")  # fmt: skip
        self.run_cli("add", "--type", "income", "--account", "shop", "--party", "buyer",
                     "--amount", "5", "--date", "2026-10-01")  # fmt: skip
        code, out, err = self.run_cli("report", "tax", "--book", "личное", "--period", "2026-10")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("своё правило", out)
        self.assertIn("KZT: база 70,00", out)
        self.assertIn("#2 доход от покупателя на счёте книги «бизнес»", out)
        self.assertNotIn("tax=нет", out)
        # период без [tax.*] принимается в любом виде
        code, _, err = self.run_cli("report", "tax", "--book", "личное", "--period", "2026-Q4")
        self.assertEqual((code, err), (0, ""))

    def test_period_with_year_zero_is_a_ledger_error(self):
        for period, text in (("год", "0000"), ("месяц", "0000-01"), (None, "0000-H1")):
            with self.subTest(text=text), self.assertRaises(LedgerError):
                parse_period(text, period)

    # check

    def test_check_runs_extension_tests(self):
        self.write("demo.py", demo())
        test = """\
            import unittest
            from pathlib import Path

            import demo
            from ledger.journal import FIELDS

            class DemoTest(unittest.TestCase):
                def test_parse(self):
                    self.assertIn("date", FIELDS)
                    self.assertEqual(len(demo.parse(Path("x"))["entries"]), EXPECTED)
            """
        self.write("test_demo.py", test.replace("EXPECTED", "3"))
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertIn("Тесты расширений не прошли", out)
        self.write("test_demo.py", test.replace("EXPECTED", "2"))
        code, out, err = self.run_cli("check")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Тесты расширений прошли", out)
        self.assertEqual(list(self.data.extensions.glob("__pycache__")), [])

    def test_check_restores_files_that_extension_tests_change(self):
        self.write(
            "writer.py",
            """\
            from pathlib import Path

            (Path(__file__).parent.parent / "journal.csv").write_text("x", encoding="utf-8")

            def register(api):
                pass
            """,
        )
        self.write(
            "test_writer.py",
            """\
            import unittest
            from pathlib import Path

            import writer
            from ledger.journal import locked

            class WriterTest(unittest.TestCase):
                def test_lock_is_free_while_tests_run(self):
                    with locked(Path(writer.__file__).parent.parent / "journal.csv"):
                        pass
            """,
        )
        before = self.data.journal.read_bytes()
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("тесты расширения writer изменили journal.csv мимо основы", out)
        # файлы мог менять не только тест: сказать прямо, что откат захватил всё за время проверки
        self.assertIn(
            "все изменения за время проверки, в том числе со страницы, отменены: "
            "если правили вы, повторите",
            out,
        )
        self.assertEqual(self.data.journal.read_bytes(), before)

    def test_check_without_extension_tests_stays_quiet(self):
        self.write("demo.py", demo())
        code, out, err = self.run_cli("check")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("Тесты расширений", out)

    def test_check_fails_on_a_broken_extension(self):
        self.write("bad.py", "raise RuntimeError('сломалось')\n")
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("Внимание: ", err)

    # Kaspi со счетами

    def import_kaspi(self, *argv):
        text = FIXTURE.read_text(encoding="utf-8")
        with mock.patch("ledger.kaspi.extract_text", return_value=text):
            return self.run_cli("import-kaspi", "statement.pdf", *argv)

    def test_kaspi_takes_the_single_kaspi_account(self):
        code, _, err = self.import_kaspi()
        self.assertEqual(code, 0, err)
        self.assertEqual({e.account for e in self.load()}, {"card"})

    def test_kaspi_needs_account_when_it_is_ambiguous(self):
        two = TOML.replace('import = "demo"', 'import = "kaspi"')
        self.data.settings_file.write_text(two, encoding="utf-8")
        before = self.data.journal.read_bytes()
        code, _, err = self.import_kaspi()
        self.assertEqual(code, 1)
        self.assertIn("укажите --account", err)
        self.assertEqual(self.data.journal.read_bytes(), before)
        code, _, err = self.import_kaspi("--account", "shop")
        self.assertEqual(code, 0, err)
        self.assertEqual({e.account for e in self.load()}, {"shop"})

    def test_import_needs_account_when_no_account_has_the_importer(self):
        self.write("demo.py", demo())
        none = TOML.replace('import = "demo"', 'import = "kaspi"')
        self.data.settings_file.write_text(none, encoding="utf-8")
        code, _, err = self.import_demo()
        self.assertEqual(code, 1)
        self.assertIn("укажите --account", err)
        code, _, err = self.import_demo("--account", "card")
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
