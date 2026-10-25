import subprocess
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from ledger import kaspi
from ledger.journal import validate
from tests.helpers import make_settings

D = Decimal
FIXTURE = Path(__file__).parent / "fixtures" / "kaspi_sample.txt"
RULES = [("playbox", "развлечения"), ("lumen", "кафе и доставка")]


def sample_text():
    return FIXTURE.read_text(encoding="utf-8")


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.st = kaspi.parse_statement(sample_text())

    def test_header(self):
        self.assertEqual(self.st.period_from, date(2026, 10, 1))
        self.assertEqual(self.st.period_to, date(2026, 10, 3))
        self.assertEqual(self.st.opening, D("10000.00"))
        self.assertEqual(self.st.closing, D("31106.39"))
        self.assertEqual(self.st.summary["Покупки"], D("-8399.61"))
        self.assertEqual(self.st.summary["Переводы на свои счета"], D("0.00"))

    def test_rows_across_page_break(self):
        self.assertEqual(len(self.st.rows), 9)
        self.assertEqual(
            self.st.rows[6],
            kaspi.KaspiRow(date(2026, 10, 1), D("-1000.00"), "Покупка", "Lumen Coffee, 2 mkr"),
        )

    def test_foreign_amount_and_explanation_go_to_note(self):
        self.assertEqual(self.st.rows[0].note, "149,50 TRY")
        self.assertEqual(self.st.rows[5].amount, D("21.70"))
        self.assertEqual(self.st.rows[5].note, "Курсовая разница")

    def test_form_feed_between_pages_is_ignored(self):
        text = sample_text().replace(
            "\nПриложение к Справке №0000000000 от 03 октября 2026\n\n  01.10",
            "\n\fПриложение к Справке №0000000000 от 03 октября 2026\n\n  01.10",
        )
        self.assertEqual(len(kaspi.parse_statement(text).rows), 9)

    def test_details_whitespace_is_normalised(self):
        text = sample_text().replace("Lumen Coffee, 2 mkr", "Lumen   Coffee,  2 mkr")
        self.assertEqual(kaspi.parse_statement(text).rows[6].details, "Lumen Coffee, 2 mkr")

    def test_unparsable_dated_line_is_an_error(self):
        text = sample_text().replace("Перевод      Борис С.", "Кэшбэк      Борис С.")
        with self.assertRaisesRegex(kaspi.KaspiError, "02.10.26"):
            kaspi.parse_statement(text)

    def test_amount_line_that_is_not_a_row_is_an_error_not_a_note(self):
        text = sample_text().replace(
            "02.10.26          - 2 000,00 ₸", "02/10/26          - 2 000,00 ₸"
        )
        with self.assertRaisesRegex(kaspi.KaspiError, "02/10/26"):
            kaspi.parse_statement(text)

    def test_repeated_table_header_is_skipped(self):
        header = "   Дата            Сумма                  Операция        Детали\n"
        text = sample_text().replace(
            "  01.10.26          - 1 000,00 ₸", header + "  01.10.26          - 1 000,00 ₸"
        )
        st = kaspi.parse_statement(text)
        self.assertEqual(len(st.rows), 9)
        self.assertEqual(st.rows[5].note, "Курсовая разница")

    def test_other_documents_are_rejected(self):
        with self.assertRaisesRegex(kaspi.KaspiError, "не похоже на выписку Kaspi"):
            kaspi.parse_statement("Счёт на оплату №5\nИтого 100,00 ₸\n")


class ExtractTest(unittest.TestCase):
    def test_unreadable_file_gets_a_short_reason(self):
        failure = subprocess.CalledProcessError(
            1, "pdftotext", stderr="Syntax Warning: x\nSyntax Error: Couldn't read xref table\n"
        )
        with mock.patch("ledger.kaspi.subprocess.run", side_effect=failure):
            with self.assertRaisesRegex(
                kaspi.KaspiError,
                r"^файл не читается как PDF \(Syntax Error: Couldn't read xref table\)$",
            ):
                kaspi.extract_text(Path("statement.pdf"))

    def test_hung_converter_is_stopped(self):
        hung = subprocess.TimeoutExpired("pdftotext", 1)
        with mock.patch("ledger.kaspi.subprocess.run", side_effect=hung) as run:
            with self.assertRaisesRegex(kaspi.KaspiError, "слишком долго"):
                kaspi.extract_text(Path("statement.pdf"))
        self.assertGreater(run.call_args.kwargs["timeout"], 0)

    def test_text_that_is_not_utf8_is_a_kaspi_error(self):
        broken = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with mock.patch("ledger.kaspi.subprocess.run", side_effect=broken):
            with self.assertRaisesRegex(kaspi.KaspiError, "не читается"):
                kaspi.extract_text(Path("statement.pdf"))


class ReconcileTest(unittest.TestCase):
    def test_sample_reconciles(self):
        self.assertEqual(kaspi.reconcile(kaspi.parse_statement(sample_text())), [])

    def test_changed_row_breaks_closing_and_group(self):
        text = sample_text().replace(
            "- 2 000,00 ₸               Перевод", "- 2 500,00 ₸               Перевод"
        )
        problems = kaspi.reconcile(kaspi.parse_statement(text))
        self.assertEqual(len(problems), 2)
        self.assertIn("остаток на конец", problems[0])
        self.assertIn("Переводы", problems[1])

    def test_unsupported_group_is_reported(self):
        text = sample_text().replace(
            " Зачисления кредитов                          + 0,00 ₸",
            " Зачисления кредитов                      + 5 000,00 ₸",
        )
        problems = kaspi.reconcile(kaspi.parse_statement(text))
        self.assertTrue(any("Зачисления кредитов" in p for p in problems))


class ConvertTest(unittest.TestCase):
    def setUp(self):
        self.st = kaspi.parse_statement(sample_text())
        self.entries = kaspi.to_entries(kaspi.new_rows(self.st, []), RULES, start_id=10)

    def by_description(self, text):
        return [e for e in self.entries if e.description == text]

    def test_entries_are_chronological_with_sequential_ids(self):
        self.assertEqual([e.id for e in self.entries], list(range(10, 19)))
        self.assertEqual(self.entries[0].date, date(2026, 10, 1))
        self.assertEqual(self.entries[-1].date, date(2026, 10, 3))

    def test_every_entry_is_valid_kzt_from_kaspi(self):
        for e in self.entries:
            self.assertEqual(validate(e, make_settings()), [])
            self.assertEqual((e.currency, e.source), ("KZT", "kaspi-pdf"))

    def test_operation_mapping(self):
        purchase = self.by_description("playbox")[0]
        self.assertEqual(
            (purchase.type, purchase.amount, purchase.category, purchase.note),
            ("expense", D("6421.31"), "развлечения", "149,50 TRY"),
        )
        refund = self.by_description("StreamBox")[0]
        self.assertEqual(
            (refund.type, refund.amount, refund.category), ("expense", D("-21.70"), "прочее")
        )
        transfer = self.by_description("Борис С.")[0]
        self.assertEqual(
            (transfer.type, transfer.amount, transfer.category),
            ("expense", D("2000.00"), "переводы"),
        )
        top_up = self.by_description("Анна К.")[0]
        self.assertEqual(
            (top_up.type, top_up.amount, top_up.category),
            ("income", D("50000.00"), "входящие переводы"),
        )
        cash = self.by_description("Банкомат ТЦ Центральный")[0]
        self.assertEqual(
            (cash.type, cash.amount, cash.category), ("cash_withdrawal", D("18000.00"), "")
        )
        fee = self.by_description("Комиссия за перевод в другой банк")[0]
        self.assertEqual((fee.type, fee.amount, fee.category), ("expense", D("494.00"), "прочее"))

    def test_remembered_exact_rule_overrides_the_fixed_transfer_category(self):
        rules = RULES + [("=борис с.", "жильё"), ("анна", "подарки")]
        entries = kaspi.to_entries(kaspi.new_rows(self.st, []), rules, start_id=1)
        by_description = {e.description: e.category for e in entries}
        self.assertEqual(by_description["Борис С."], "жильё")
        self.assertEqual(by_description["Анна К."], "входящие переводы")
        self.assertEqual(by_description["Комиссия за перевод в другой банк"], "прочее")

    def test_reimport_adds_nothing(self):
        self.assertEqual(kaspi.new_rows(self.st, self.entries), [])
        self.assertEqual(kaspi.missing_entries(self.st, self.entries), [])

    def test_identical_rows_are_counted_not_collapsed(self):
        self.assertEqual(len(self.by_description('ТОО "ROMASHKA SHOP"')), 2)
        without_one = [
            e for e in self.entries if e.id != self.by_description('ТОО "ROMASHKA SHOP"')[0].id
        ]
        added = kaspi.new_rows(self.st, without_one)
        self.assertEqual([r.details for r in added], ['ТОО "ROMASHKA SHOP"'])

    def test_edited_entry_still_matches_its_statement_row(self):
        purchase = self.by_description("playbox")[0]
        purchase.description = "игра"
        purchase.shares = {"anya": 50}
        self.assertEqual(kaspi.new_rows(self.st, self.entries), [])

    def test_changed_amount_shows_up_as_new_row_and_missing_entry(self):
        text = (
            sample_text()
            .replace("- 6 421,31 ₸", "- 6 511,31 ₸")
            .replace("- 8 399,61 ₸", "- 8 489,61 ₸")
            .replace("+ 31 106,39 ₸", "+ 31 016,39 ₸")
        )
        later = kaspi.parse_statement(text)
        self.assertEqual(kaspi.reconcile(later), [])
        self.assertEqual([r.amount for r in kaspi.new_rows(later, self.entries)], [D("-6511.31")])
        self.assertEqual(
            [e.amount for e in kaspi.missing_entries(later, self.entries)], [D("6421.31")]
        )

    def test_manual_entries_are_never_reported_missing(self):
        manual = kaspi.to_entry(self.st.rows[0], RULES, 99)
        manual.source, manual.source_ref = "text", ""
        self.assertEqual(kaspi.missing_entries(self.st, self.entries + [manual]), [])


if __name__ == "__main__":
    unittest.main()
