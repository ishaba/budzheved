import contextlib
import io
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from ledger import cli, ops
from ledger.journal import JournalError
from ledger.journal import load as load_journal
from tests.helpers import ANYA, MINIMAL, make_data

FIXTURE = Path(__file__).parent / "fixtures" / "kaspi_sample.txt"
BORIS = '[people.boris]\nname = "Борис"\nrole = "партнёр"\nsettlements = true\n'
# Валюта владельца KZT, две книги, счета card (KZT) и paypal (USD).
ACCOUNTS = (
    MINIMAL.replace('currency = "USDT"', 'currency = "KZT"')
    + '[books]\ndefault = "личное"\nnames = ["личное", "бизнес"]\n'
    + '[accounts.card]\nbook = "личное"\ncurrencies = ["KZT"]\n'
    + '[accounts.paypal]\nbook = "бизнес"\ncurrencies = ["USD"]\n'
)


class ParseIdsTest(unittest.TestCase):
    def test_lists_ranges_spaces_and_dashes(self):
        self.assertEqual(cli.parse_ids("3,7,12-15"), [3, 7, 12, 13, 14, 15])
        self.assertEqual(cli.parse_ids("3, 7, 12–15"), [3, 7, 12, 13, 14, 15])
        self.assertEqual(cli.parse_ids("4"), [4])

    def test_rejects_garbage(self):
        for text in ("", "a", "5-3", "1-", "1-2-3"):
            with self.subTest(text=text), self.assertRaises(JournalError):
                cli.parse_ids(text)


class SharesHelpTest(unittest.TestCase):
    def test_example_with_semicolon_is_quoted(self):
        self.assertIn('"anya:25;boris:25"', cli.SHARES_HELP)


class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = make_data(Path(tmp.name) / "Мои деньги", ANYA)
        self.journal = self.data.journal
        self.empty = self.journal.read_bytes()
        self.data.merchants.write_text(
            "pattern,category\nтакси,транспорт\nplaybox,развлечения\n", encoding="utf-8"
        )

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["--data", str(self.data.root), *argv])
            except SystemExit as exc:  # argparse отказывает с кодом 2
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def load(self):
        return load_journal(self.journal, self.data.settings)

    def add(self, *argv):
        code, out, err = self.run_cli("add", "--date", "2026-10-01", *argv)
        self.assertEqual(code, 0, err + out)
        return out

    def test_add_creates_journal_with_defaults(self):
        out = self.add("--amount", "12", "--desc", "такси до дома")
        self.assertIn("#1 2026-10-01 расход 12,00 USDT [транспорт] такси до дома", out)
        (entry,) = self.load()
        self.assertEqual((entry.paid_by, entry.shares, entry.source), ("me", {}, "text"))

    def test_add_accepts_human_amount_and_lowercase_currency(self):
        self.add("--amount", "1 500,50", "--currency", "kzt", "--desc", "кофе")
        (entry,) = self.load()
        self.assertEqual(
            (entry.amount, entry.currency, entry.category), (Decimal("1500.50"), "KZT", "прочее")
        )

    def test_add_income_exchange_and_anya_payment(self):
        self.add("--type", "income", "--amount", "+2000", "--desc", "зп")
        self.add(
            "--type", "exchange", "--amount", "100", "--amount2", "45000", "--currency2", "kzt"
        )
        self.add(
            "--amount",
            "8000",
            "--currency",
            "KZT",
            "--paid-by",
            "anya",
            "--shares",
            "50",
            "--desc",
            "ужин",
        )
        income, exchange, dinner = self.load()
        self.assertEqual(income.category, "доход")
        self.assertEqual((exchange.amount2, exchange.currency2), (Decimal("45000"), "KZT"))
        self.assertEqual((dinner.paid_by, dinner.shares), ("anya", {"anya": 50}))

    def test_settlement_gets_the_single_person_as_party(self):
        out = self.add("--type", "settlement", "--amount", "300", "--paid-by", "anya")
        self.assertIn("расчёт 300,00 USDT (с Аня) (платил Аня)", out)
        (entry,) = self.load()
        self.assertEqual(entry.party, "anya")

    def use(self, toml):
        self.data = make_data(self.data.root.parent / "другая", toml)
        self.journal = self.data.journal

    def help_text(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as done:
            cli.main(["--data", str(self.data.root), *argv, "--help"])
        self.assertEqual(done.exception.code, 0)
        return out.getvalue()

    def test_help_shows_settlements_only_when_enabled(self):
        self.assertIn("share", self.help_text())
        self.assertIn("debt", self.help_text("report"))
        add = self.help_text("add")
        for flag in ("--shares", "--paid-by", "--party", "--tax", "--receipt"):
            self.assertIn(flag, add)
        for flag in ("--account", "--book", "transfer"):
            self.assertNotIn(flag, add)
        self.use(MINIMAL)
        self.assertNotIn("share", self.help_text())
        self.assertNotIn("debt", self.help_text("report"))
        add = self.help_text("add")
        for flag in ("--shares", "--paid-by", "--party", "--account", "--book"):
            self.assertNotIn(flag, add)
        self.assertIn("--receipt", add)

    def test_hidden_settlements_are_refused(self):
        self.use(MINIMAL)
        self.add("--amount", "10")
        for argv in (
            ("report", "debt"),
            ("share", "1"),
            ("add", "--amount", "5", "--shares", "50"),
            ("add", "--amount", "5", "--paid-by", "anya"),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(
                    err,
                    "Ошибка: взаиморасчёты выключены: в settings.toml нет людей "
                    "с settlements = true\n",
                )
        self.assertEqual(len(self.load()), 1)

    def test_hidden_accounts_and_books_are_refused(self):
        for argv, words in (
            (("add", "--amount", "5", "--account", "card"), "счета выключены"),
            (("add", "--amount", "5", "--account2", "card"), "счета выключены"),
            (("add", "--type", "transfer", "--amount", "5"), "счета выключены"),
            (("list", "--account", "card"), "счета выключены"),
            (("add", "--amount", "5", "--book", "бизнес"), "книги выключены"),
            (("report", "categories", "--book", "бизнес"), "книги выключены"),
        ):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 1)
                self.assertIn(words, err)
        _, _, err = self.run_cli("list", "--book", "x")
        self.assertIn("Ошибка: книги выключены: в settings.toml нет [books]", err)
        _, _, err = self.run_cli("list", "--account", "x")
        self.assertIn("Ошибка: счета выключены: в settings.toml нет [accounts]", err)
        self.assertEqual(self.journal.read_bytes(), self.empty)

    def test_abbreviated_subcommand_flag_is_not_taken_for_data(self):
        code, _, err = self.run_cli("add", "--da", "2026-10-02", "--amount", "5", "--shares", "50")
        self.assertEqual(code, 0, err)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(
                ["--dat", str(self.data.root), "add", "--amount", "6", "--shares", "50"]
            )
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(
            [(str(e.date), e.shares) for e in self.load()][0], ("2026-10-02", {"anya": 50})
        )
        self.assertEqual(len(self.load()), 2)

    def test_parser_without_settings_still_gives_help(self):
        help_text = io.StringIO()
        with contextlib.redirect_stdout(help_text), self.assertRaises(SystemExit):
            cli.build_parser(None).parse_args(["add", "--help"])
        self.assertIn("--amount", help_text.getvalue())
        self.assertNotIn("--shares", help_text.getvalue())
        self.data.settings_file.unlink()
        code, _, err = self.run_cli("report", "balance")
        self.assertEqual(code, 1)
        self.assertIn("Нет файла настроек", err)

    def test_accounts_balances_and_books(self):
        self.use(ACCOUNTS)
        self.assertIn("--account", self.help_text("add"))
        self.assertIn("transfer", self.help_text("add"))
        self.assertIn("--book", self.help_text("report"))
        self.add("--type", "opening_balance", "--account", "card", "--amount", "700")
        self.add("--type", "income", "--account", "paypal", "--amount", "10", "--currency", "USD")
        self.add("--amount", "300", "--category", "продукты")
        card, paypal, cash = self.load()
        # Книгу счёта запись не хранит: её даёт счёт.
        self.assertEqual((card.currency, card.book), ("KZT", ""))
        self.assertEqual((paypal.account, paypal.book), ("paypal", ""))
        self.assertEqual((cash.account, cash.book, cash.currency), ("", "", "KZT"))
        _, out, _ = self.run_cli("report", "balance")
        self.assertIn("  card, KZT: 700,00\n  paypal, USD: +10,00", out)
        self.assertIn("  без счёта, KZT: -300,00", out)
        _, out, _ = self.run_cli("report", "categories", "--month", "2026-10", "--book", "бизнес")
        self.assertIn("Доходы за 2026-10, книга бизнес:\n  USD\n    доход: 10,00", out)
        self.assertNotIn("продукты", out)
        _, out, _ = self.run_cli("list", "--account", "paypal")
        self.assertEqual(out.count("\n"), 1)
        self.assertIn("(счёт paypal)", out)
        _, out, _ = self.run_cli("list", "--book", "личное")
        self.assertEqual(out.count("\n"), 2)

    def test_account_currency_is_checked(self):
        self.use(ACCOUNTS)
        code, _, err = self.run_cli(
            "add", "--account", "paypal", "--amount", "10", "--currency", "KZT"
        )
        self.assertEqual(code, 1)
        self.assertIn("валюты KZT нет у счёта 'paypal' (USD)", err)
        code, _, err = self.run_cli("add", "--account", "bank", "--amount", "10")
        self.assertEqual(code, 2)
        self.assertEqual(self.journal.read_bytes(), self.empty)

    def test_currency_defaults_to_the_owner_currency(self):
        self.use(ACCOUNTS)
        self.add("--amount", "10")
        self.assertEqual(self.load()[0].currency, "KZT")

    def test_tax_receipt_and_party(self):
        self.add("--amount", "10", "--tax", "нет", "--receipt", "2026/чек.jpg")
        self.add("--type", "debt", "--amount", "5", "--party", "anya")
        expense, debt = self.load()
        self.assertEqual((expense.tax, expense.receipt), ("нет", "2026/чек.jpg"))
        self.assertEqual((debt.party, debt.paid_by), ("anya", "me"))
        code, _, _ = self.run_cli("add", "--amount", "10", "--tax", "может")
        self.assertEqual(code, 2)

    def test_two_people_need_explicit_shares(self):
        self.use(ANYA + BORIS)
        self.add("--amount", "100", "--shares", "anya:25;boris:25")
        self.assertEqual(self.load()[0].shares, {"anya": 25, "boris": 25})
        self.assertIn("anya:25;boris:25", self.journal.read_text(encoding="utf-8"))
        for argv in (("share", "1", "--shares", "50"), ("share", "1")):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 1)
                self.assertIn("укажите, чьи доли", err)
        code, _, err = self.run_cli("add", "--amount", "7", "--shares", "50")
        self.assertEqual(code, 1)
        self.assertIn("укажите, чьи доли", err)
        code, out, _ = self.run_cli("share", "1", "--shares", "boris:30")
        self.assertEqual(code, 0)
        self.assertIn("(доли: Борис 30%)", out)
        code, _, err = self.run_cli("add", "--type", "settlement", "--amount", "5")
        self.assertEqual(code, 1)
        self.assertIn("нужен человек в party", err)
        self.add("--type", "settlement", "--amount", "5", "--party", "boris")
        _, out, _ = self.run_cli("report", "debt")
        self.assertIn("Долг:\n  Борис, USDT: долг мне 35,00\nОперации", out)

    def test_schema_1_folder_refuses_writes(self):
        self.data.settings_file.write_text(
            ANYA.replace("schema = 2", "schema = 1"), encoding="utf-8"
        )
        code, _, err = self.run_cli("add", "--amount", "10")
        self.assertEqual(code, 1)
        self.assertIn("Ошибка: журнал схемы 1: выполните budget.py migrate", err)
        self.assertEqual(self.journal.read_bytes(), self.empty)

    def test_invalid_entry_is_not_written(self):
        code, _, err = self.run_cli("add", "--amount", "abc")
        self.assertEqual(code, 1)
        self.assertIn("Ошибка: не число", err)
        code, _, err = self.run_cli("add", "--amount", "5", "--currency", "euro")
        self.assertEqual(code, 1)
        self.assertIn("неизвестная валюта", err)
        self.assertEqual(self.journal.read_bytes(), self.empty)

    def test_probable_duplicate_needs_force(self):
        self.add("--amount", "12", "--desc", "такси")
        code, out, _ = self.run_cli(
            "add", "--date", "2026-10-02", "--amount", "12", "--desc", "такси"
        )
        self.assertEqual(code, 1)
        self.assertIn("Похоже на дубль", out)
        self.assertIn("#1 ", out)
        self.assertEqual(len(self.load()), 1)
        self.add("--amount", "12", "--desc", "такси", "--force")
        self.assertEqual(len(self.load()), 2)

    def test_share_marks_listed_expenses(self):
        for amount in ("10", "20", "30", "40"):
            self.add("--amount", amount)
        code, out, _ = self.run_cli("share", "1,", "3-4")
        self.assertEqual(code, 0)
        self.assertEqual([sum(e.shares.values()) for e in self.load()], [50, 0, 50, 50])
        self.assertEqual(out.count("(доли: Аня 50%)"), 3)
        self.run_cli("share", "1", "--shares", "0")
        self.assertEqual(self.load()[0].shares, {})
        self.run_cli("share", "2", "--shares", "anya:30")
        self.assertEqual(self.load()[1].shares, {"anya": 30})

    def test_share_changes_nothing_on_bad_ids(self):
        self.add("--amount", "10")
        self.add("--type", "income", "--amount", "20")
        code, _, err = self.run_cli("share", "1,5")
        self.assertEqual(code, 1)
        self.assertIn("нет записей с номерами: 5", err)
        code, _, err = self.run_cli("share", "1-2")
        self.assertEqual(code, 1)
        self.assertIn("не расходы: 2", err)
        self.assertEqual([e.shares for e in self.load()], [{}, {}])

    def test_edit_updates_and_revalidates(self):
        self.add("--amount", "10", "--desc", "кофе")
        code, out, _ = self.run_cli(
            "edit", "1", "category=кафе и доставка", "amount=1 250,5", "currency=KZT"
        )
        self.assertEqual(code, 0)
        self.assertIn("1 250,50 KZT [кафе и доставка]", out)
        code, _, err = self.run_cli("edit", "1", "category=метро")
        self.assertEqual(code, 1)
        self.assertIn("неизвестная категория", err)
        code, _, err = self.run_cli("edit", "1", "id=7")
        self.assertEqual(code, 1)
        self.assertEqual(self.load()[0].category, "кафе и доставка")

    def test_list_filters(self):
        self.add("--amount", "10", "--desc", "раз")
        self.run_cli("add", "--date", "2026-10-05", "--amount", "20", "--desc", "два")
        _, out, _ = self.run_cli("list", "--from", "2026-10-03")
        self.assertNotIn("раз", out)
        self.assertIn("два", out)

    def test_reports(self):
        self.add("--type", "debt", "--amount", "850", "--paid-by", "anya")
        self.add("--amount", "6421.31", "--currency", "KZT", "--desc", "playbox", "--shares", "50")
        _, out, _ = self.run_cli("report", "debt")
        self.assertIn("Аня, USDT: мой долг 850,00", out)
        self.assertIn("Аня, KZT: долг мне 3 210,66", out)
        self.assertIn("долг 850,00 USDT (с Аня) (кредитор Аня)", out)
        _, out, _ = self.run_cli("report", "balance")
        self.assertIn("KZT: -6 421,31 (без стартового остатка", out)
        _, out, _ = self.run_cli("report", "categories", "--month", "2026-10")
        self.assertIn("развлечения: 3 210,66", out)

    def test_custom_category_rule_works_for_add_report_and_check(self):
        self.data.settings_file.write_text(
            ANYA + '[categories]\nexpense = ["сырьё"]\n', encoding="utf-8"
        )
        self.add("--amount", "10", "--desc", "Мука", "--category", "прочее")
        ops.apply_merchant_category(self.data, "Мука", "сырьё")
        out = self.add("--amount", "15", "--desc", "мука", "--date", "2026-10-05")
        self.assertIn("[сырьё] мука", out)
        code, out, err = self.run_cli("report", "categories", "--month", "2026-10")
        self.assertEqual(code, 0, err)
        self.assertIn("сырьё: 25,00", out)
        code, out, err = self.run_cli("check")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Журнал в порядке: записей 2", out)

    def test_lending_and_borrowing_from_agents_move_cash_and_debt(self):
        self.add("--type", "opening_balance", "--amount", "1000")
        self.add("--type", "settlement", "--party", "anya", "--amount", "100", "--desc", "одолжил")
        _, out, _ = self.run_cli("report", "balance")
        self.assertIn("USDT: 900,00", out)
        _, out, _ = self.run_cli("report", "debt")
        self.assertIn("Аня, USDT: долг мне 100,00", out)
        self.add("--type", "settlement", "--paid-by", "anya", "--amount", "300", "--desc", "занял")
        _, out, _ = self.run_cli("report", "balance")
        self.assertIn("USDT: 1 200,00", out)
        _, out, _ = self.run_cli("report", "debt")
        self.assertIn("Аня, USDT: мой долг 200,00", out)

    def test_check_reports_broken_journal(self):
        self.add("--amount", "10")
        code, out, _ = self.run_cli("check")
        self.assertEqual((code, out), (0, "Журнал в порядке: записей 1\n"))
        self.journal.write_text(
            self.journal.read_text(encoding="utf-8").replace("USDT", "EURO"), encoding="utf-8"
        )
        code, _, err = self.run_cli("report", "balance")
        self.assertEqual(code, 1)
        self.assertIn("строка 2 (запись #1): неизвестная валюта", err)

    def test_check_names_a_person_missing_from_settings(self):
        self.add("--amount", "10", "--shares", "50")
        self.data.settings_file.write_text(MINIMAL, encoding="utf-8")
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("запись #1", err)
        self.assertIn("человек 'anya' не найден в settings.toml", err)

    def import_sample(self, *argv, text=None):
        text = FIXTURE.read_text(encoding="utf-8") if text is None else text
        with mock.patch("ledger.kaspi.extract_text", return_value=text):
            return self.run_cli("import-kaspi", "statement.pdf", *argv)

    def test_import_dry_run_writes_nothing(self):
        code, out, _ = self.import_sample("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("строк 9, новых 9, сверка сошлась на 31 106,39 ₸", out)
        self.assertIn("Пробный запуск", out)
        self.assertEqual(self.journal.read_bytes(), self.empty)

    def test_import_then_reimport(self):
        self.add("--amount", "850", "--type", "debt", "--paid-by", "anya")
        code, out, _ = self.import_sample()
        self.assertEqual(code, 0)
        entries = self.load()
        self.assertEqual(len(entries), 10)
        self.assertEqual(entries[1].id, 2)
        self.assertIn("#10 2026-10-03 расход 6 421,31 KZT [развлечения] playbox (149,50 TRY)", out)
        code, out, _ = self.import_sample()
        self.assertIn("новых 0", out)
        self.assertEqual(len(self.load()), 10)

    def test_import_that_does_not_reconcile_writes_nothing(self):
        text = FIXTURE.read_text(encoding="utf-8").replace(
            "- 494,00 ₸                Разное", "- 400,00 ₸                Разное"
        )
        code, out, _ = self.import_sample(text=text)
        self.assertEqual(code, 1)
        self.assertIn("Выписка не сошлась, ничего не записано", out)
        self.assertEqual(self.journal.read_bytes(), self.empty)

    def test_import_with_unknown_operation_writes_nothing(self):
        text = FIXTURE.read_text(encoding="utf-8").replace(
            "Перевод      Борис С.", "Кэшбэк      Борис С."
        )
        code, _, err = self.import_sample(text=text)
        self.assertEqual(code, 1)
        self.assertIn("не удалось разобрать строку выписки", err)
        self.assertEqual(self.journal.read_bytes(), self.empty)

    def test_import_points_at_entries_already_typed_by_hand(self):
        self.add("--amount", "1000", "--currency", "KZT", "--desc", "кофе Lumen")
        code, out, _ = self.import_sample()
        self.assertEqual(code, 0)
        self.assertIn("Возможно, уже записано вручную", out)
        self.assertIn("новая #4 ↔ #1 2026-10-01 расход 1 000,00 KZT [прочее] кофе Lumen", out)
        self.assertEqual(len(self.load()), 10)

    def test_changed_blocked_amount_is_listed_and_resolved_with_delete(self):
        self.import_sample()
        changed = (
            FIXTURE.read_text(encoding="utf-8")
            .replace("- 6 421,31 ₸", "- 6 511,31 ₸")
            .replace("- 8 399,61 ₸", "- 8 489,61 ₸")
            .replace("+ 31 106,39 ₸", "+ 31 016,39 ₸")
        )
        code, out, _ = self.import_sample(text=changed)
        self.assertEqual(code, 0)
        self.assertIn("новых 1", out)
        self.assertIn("Есть в журнале, но нет в этой выписке", out)
        self.assertIn("  #9 2026-10-03 расход 6 421,31 KZT", out)
        code, out, _ = self.run_cli("delete", "9")
        self.assertEqual(code, 0)
        self.assertIn("Удалено: #9 2026-10-03 расход 6 421,31 KZT", out)
        _, out, _ = self.run_cli("report", "balance")
        self.assertIn("KZT: +39 016,39", out)
        _, out, _ = self.import_sample(text=changed)
        self.assertIn("новых 0", out)
        self.assertNotIn("Есть в журнале", out)

    def test_delete_changes_nothing_on_unknown_id(self):
        self.add("--amount", "10")
        code, _, err = self.run_cli("delete", "1,4")
        self.assertEqual(code, 1)
        self.assertIn("нет записей с номерами: 4", err)
        self.assertEqual(len(self.load()), 1)

    def test_import_reports_missing_pdftotext(self):
        with mock.patch("ledger.kaspi.subprocess.run", side_effect=FileNotFoundError):
            code, _, err = self.run_cli("import-kaspi", "statement.pdf")
        self.assertEqual(code, 1)
        self.assertIn("pdftotext из Poppler", err)


if __name__ == "__main__":
    unittest.main()
