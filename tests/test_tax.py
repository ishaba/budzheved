import contextlib
import io
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from ledger import cli, reports
from ledger.journal import Entry, LedgerError
from ledger.tax import TaxResult, compute, parse_period
from tests.helpers import make_data, make_settings

D = Decimal

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
currencies = ["KZT", "USD"]

[accounts.shop]
book = "бизнес"
currencies = ["KZT", "USD"]

[people.buyer]
name = "Покупатель"
role = "покупатель"

[people.boris]
name = "Борис"
role = "партнёр"
settlements = true

[tax."бизнес"]
rate = 3
base = "доходы"
period = "полугодие"
categories = ["продажи"]

[categories]
expense = ["сырьё"]
income = ["продажи", "доход"]
"""
S = make_settings(TOML)
EXPENSES = TOML.replace('base = "доходы"', 'base = "доходы-расходы"')
SE = make_settings(EXPENSES)
START, END = date(2026, 7, 1), date(2026, 12, 31)


def entry(**overrides):
    base = dict(
        id=1,
        date=date(2026, 10, 1),
        type="income",
        amount=D("1000"),
        currency="KZT",
        book="бизнес",
        category="продажи",
    )
    base.update(overrides)
    return Entry(**base)


def run(entries, s=S, book="бизнес"):
    return compute(entries, s, book, START, END)


class ParsePeriodTest(unittest.TestCase):
    def test_half_year(self):
        self.assertEqual(
            parse_period("2026-H2", "полугодие"), (date(2026, 7, 1), date(2026, 12, 31))
        )
        self.assertEqual(
            parse_period("2026-H1", "полугодие"), (date(2026, 1, 1), date(2026, 6, 30))
        )

    def test_other_kinds(self):
        self.assertEqual(parse_period("2026-02", "месяц"), (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(parse_period("2028-02", "месяц")[1], date(2028, 2, 29))
        self.assertEqual(parse_period("2026-Q3", "квартал"), (date(2026, 7, 1), date(2026, 9, 30)))
        self.assertEqual(parse_period("2026", "год"), (date(2026, 1, 1), date(2026, 12, 31)))

    def test_wrong_kind_names_the_expected_one(self):
        cases = {
            ("2026-Q1", "полугодие"): "налог считается по полугодию: укажите 2026-H1 или 2026-H2",
            ("2026-H2", "квартал"): "налог считается по кварталу: укажите 2026-Q1",
            ("2026", "месяц"): "налог считается по месяцу: укажите 2026-10",
            ("2026-10", "год"): "налог считается по году: укажите 2026",
            ("2026-H3", "полугодие"): "налог считается по полугодию",
            ("2026-13", "месяц"): "налог считается по месяцу",
        }
        for (text, kind), message in cases.items():
            with self.subTest(text=text, kind=kind), self.assertRaises(LedgerError) as caught:
                parse_period(text, kind)
            self.assertIn(message, str(caught.exception))


class ComputeTest(unittest.TestCase):
    def test_income_base(self):
        result = run([entry(id=1), entry(id=2)])
        self.assertEqual(result.base, {"KZT": D("2000")})
        self.assertEqual(result.estimate, {"KZT": D("60")})
        self.assertEqual(result.needs_decision, [])
        self.assertEqual(result.note, "оценка, сверить с правилами режима или с бухгалтером")

    def test_period_and_book_limits(self):
        result = run(
            [
                entry(id=1),
                entry(id=2, date=date(2026, 6, 30)),
                entry(id=3, date=date(2027, 1, 1)),
                entry(id=4, date=date(2026, 12, 31)),
                entry(id=5, book="личное"),
            ]
        )
        self.assertEqual(result.base, {"KZT": D("2000")})

    def test_income_minus_expenses_and_refund(self):
        entries = [
            entry(id=1, amount=D("2000")),
            entry(id=2, type="expense", amount=D("500"), category="сырьё"),
            entry(id=3, type="expense", amount=D("-100"), category="сырьё"),
            entry(id=4, type="expense", amount=D("300"), category="сырьё", tax="нет"),
            entry(id=5, type="expense", amount=D("900"), category="сырьё", book="личное"),
        ]
        result = run(entries, SE)
        self.assertEqual(result.base, {"KZT": D("1500")})
        self.assertEqual(result.estimate, {"KZT": D("45")})
        self.assertEqual([i for i, _ in result.needs_decision], [3, 4])

    def test_loss_gives_zero_estimate_and_a_decision_line(self):
        entries = [
            entry(id=1, amount=D("400")),
            entry(id=2, type="expense", amount=D("1000"), category="сырьё"),
            entry(id=3, category="доход", tax="да", amount=D("10"), currency="USD"),
            entry(id=4, book="личное", party="buyer", amount=D("5")),
        ]
        result = run(entries, SE)
        self.assertEqual(result.base, {"KZT": D("-600"), "USD": D("10")})
        self.assertEqual(result.estimate, {"KZT": D("0"), "USD": D("0.3")})
        loss = (
            0,
            "KZT: база отрицательная (убыток), налог 0 — "
            "проверьте, нет ли минимального налога по правилам режима",
        )
        # строки записей по номерам, затем строки валют с номером 0
        self.assertEqual(
            result.needs_decision,
            [
                (3, "tax=да, но категория не облагается"),
                (4, "доход от покупателя на счёте книги «личное»"),
                loss,
            ],
        )

    def test_zero_base_has_no_loss_line(self):
        entries = [
            entry(id=1, amount=D("500")),
            entry(id=2, type="expense", amount=D("500"), category="сырьё"),
        ]
        result = run(entries, SE)
        self.assertEqual(result.base, {"KZT": D("0")})
        self.assertEqual(result.estimate, {"KZT": D("0")})
        self.assertEqual(result.needs_decision, [])

    def test_expense_counts_only_my_share(self):
        entries = [
            entry(id=1, amount=D("2000")),
            entry(id=2, type="expense", amount=D("500"), shares={"boris": 50}),
        ]
        self.assertEqual(run(entries, SE).base, {"KZT": D("1750")})

    def test_expenses_do_not_change_income_only_base(self):
        entries = [entry(id=1), entry(id=2, type="expense", amount=D("500"))]
        self.assertEqual(run(entries).base, {"KZT": D("1000")})

    def test_non_income_types_never_count(self):
        entries = [
            entry(id=1),
            entry(id=2, type="transfer", account="card", account2="shop"),
            entry(id=3, type="exchange", amount2=D("2"), currency2="USD"),
            entry(id=4, type="settlement", party="boris"),
            entry(id=5, type="debt", party="boris"),
            entry(id=6, type="debt_conversion", amount2=D("2"), currency2="USD", party="boris"),
            entry(id=7, type="opening_balance"),
            entry(id=8, type="cash_withdrawal"),
        ]
        result = run(entries, SE)
        self.assertEqual(result.base, {"KZT": D("1000")})
        self.assertEqual(result.needs_decision, [])

    def test_book_comes_from_the_account(self):
        result = run([entry(id=1, book="", account="shop"), entry(id=2, book="", account="card")])
        self.assertEqual(result.base, {"KZT": D("1000")})

    def test_buyer_income_on_another_books_account(self):
        entries = [
            entry(id=1),
            entry(id=12, book="", account="card", party="buyer", category="продажи"),
            entry(id=13, book="личное", party="boris"),
            entry(id=14, party="buyer"),
        ]
        result = run(entries)
        self.assertEqual(result.base, {"KZT": D("2000")})
        self.assertEqual(
            result.needs_decision,
            [(12, "доход от покупателя на счёте книги «личное»")],
        )

    def test_tax_flag_against_the_rule(self):
        entries = [
            entry(id=1),
            entry(id=2, category="доход", amount=D("400"), tax="да"),
            entry(id=3, category="продажи", amount=D("100"), tax="нет"),
            entry(id=4, category="доход", amount=D("50")),
            entry(id=5, category="продажи", amount=D("10"), tax="да"),
        ]
        result = run(entries)
        # 1000 + 400 (tax=да) + 10; 100 (tax=нет) и 50 (категория не облагается) не входят
        self.assertEqual(result.base, {"KZT": D("1410")})
        self.assertEqual(
            result.needs_decision,
            [(2, "tax=да, но категория не облагается"), (3, "tax=нет, но категория облагается")],
        )

    def test_expense_tax_mark_against_the_rule(self):
        marked = [
            entry(id=1),
            entry(id=2, type="expense", amount=D("300"), category="сырьё", tax="нет"),
            entry(id=3, type="expense", amount=D("200"), category="сырьё", tax="да"),
            entry(id=4, type="expense", amount=D("100"), category="сырьё"),
            entry(id=5, type="expense", amount=D("50"), category="сырьё", tax="нет", book="личное"),
        ]
        expenses = run(marked, SE)
        self.assertEqual(expenses.base, {"KZT": D("700")})
        self.assertEqual(expenses.needs_decision, [(2, "tax=нет, расход не вычитается из базы")])
        income_only = run(marked, S)
        self.assertEqual(income_only.base, {"KZT": D("1000")})
        self.assertEqual(
            income_only.needs_decision,
            [(3, "tax=да, но при базе «доходы» расходы не вычитаются")],
        )

    def test_refund_of_a_deductible_expense_is_a_decision(self):
        entries = [
            entry(id=1, amount=D("2000")),
            entry(id=2, type="expense", amount=D("500"), category="сырьё"),
            entry(id=3, type="expense", amount=D("-100"), category="сырьё"),
            entry(id=4, type="expense", amount=D("-40"), category="сырьё", book="личное"),
        ]
        result = run(entries, SE)
        self.assertEqual(result.base, {"KZT": D("1500")})
        self.assertEqual(
            result.needs_decision,
            [(3, "возврат не вычитается из расходов: оценка может быть занижена")],
        )
        # при базе «доходы» расходы и возвраты в базу не входят: решать нечего
        self.assertEqual(run(entries, S).needs_decision, [])

    def test_decisions_are_ordered_by_id(self):
        entries = [
            entry(id=9, category="продажи", tax="нет"),
            entry(id=3, book="личное", party="buyer"),
            entry(id=5, category="доход", tax="да"),
        ]
        self.assertEqual([i for i, _ in run(entries).needs_decision], [3, 5, 9])

    def test_two_currencies(self):
        entries = [entry(id=1), entry(id=2, amount=D("50"), currency="USD")]
        result = run(entries)
        self.assertEqual(result.base, {"KZT": D("1000"), "USD": D("50")})
        self.assertEqual(result.estimate, {"KZT": D("30"), "USD": D("1.5")})
        self.assertEqual(list(result.base), ["KZT", "USD"])

    def test_book_without_rule(self):
        with self.assertRaises(LedgerError) as caught:
            run([entry()], book="личное")
        self.assertEqual(str(caught.exception), "налог для книги «личное» не настроен")

    def test_extension_rule_replaces_the_builtin_one(self):
        seen = []

        def custom(entries, s):
            seen.append([e.id for e in entries])
            return TaxResult({"KZT": D("7")}, {"KZT": D("1")}, [], "своя оценка")

        entries = [entry(id=1), entry(id=2, book="личное"), entry(id=3, date=date(2026, 1, 1))]
        result = compute(entries, S, "бизнес", START, END, rule_fn=custom)
        self.assertEqual(result.note, "своя оценка")
        self.assertEqual(seen, [[1]])
        # расширение работает и у книги без [tax.*]
        self.assertEqual(
            compute(entries, S, "личное", START, END, rule_fn=custom).note, "своя оценка"
        )


class TaxReportTest(unittest.TestCase):
    def render(self, result):
        return reports.tax_report(result, "бизнес", "2026-H2")

    def test_report(self):
        result = TaxResult(
            {"KZT": D("1000000")},
            {"KZT": D("30000")},
            [(12, "доход от покупателя на счёте книги «личное»")],
            "оценка, сверить с правилами режима или с бухгалтером",
        )
        self.assertEqual(
            self.render(result),
            "Налог, книга «бизнес», 2026-H2 "
            "(оценка, сверить с правилами режима или с бухгалтером):\n"
            "  KZT: база 1 000 000,00, налог 30 000,00\n"
            "Требует решения:\n"
            "  #12 доход от покупателя на счёте книги «личное»",
        )

    def test_loss_line_has_no_entry_number(self):
        entries = [entry(id=2, type="expense", amount=D("100"), category="сырьё")]
        text = self.render(run(entries, SE))
        self.assertIn("  KZT: база -100,00, налог 0,00", text)
        self.assertIn(
            "Требует решения:\n  KZT: база отрицательная (убыток), налог 0 — проверьте, "
            "нет ли минимального налога по правилам режима",
            text,
        )
        self.assertNotIn("#0", text)

    def test_nothing_to_decide_prints_no_block(self):
        result = run([entry()])
        text = self.render(result)
        self.assertNotIn("Требует решения", text)
        self.assertIn("  KZT: база 1 000,00, налог 30,00", text)

    def test_no_income(self):
        self.assertIn("облагаемых доходов нет", self.render(run([])))


class TaxCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = make_data(Path(tmp.name) / "Мои деньги", TOML)

    def run_cli(self, *argv, data=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["--data", str((data or self.data).root), *argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_report_tax(self):
        for amount in ("1000", "1000"):
            code, _, err = self.run_cli(
                "add", "--type", "income", "--date", "2026-10-01", "--amount", amount,
                "--book", "бизнес", "--category", "продажи", "--force",
            )  # fmt: skip
            self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("report", "tax", "--book", "бизнес", "--period", "2026-H2")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Налог, книга «бизнес», 2026-H2", out)
        self.assertIn("  KZT: база 2 000,00, налог 60,00", out)

    def test_wrong_period_kind(self):
        code, _, err = self.run_cli("report", "tax", "--book", "бизнес", "--period", "2026-Q1")
        self.assertEqual(code, 1)
        self.assertIn("налог считается по полугодию: укажите 2026-H1 или 2026-H2", err)

    def test_needs_book_and_period(self):
        for argv in (("--period", "2026-H2"), ("--book", "бизнес")):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli("report", "tax", *argv)
                self.assertEqual(code, 1)
                self.assertIn("--book и --period", err)

    def test_book_without_rule_is_not_configured(self):
        code, _, err = self.run_cli("report", "tax", "--book", "личное", "--period", "2026-H2")
        self.assertEqual(code, 1)
        self.assertIn("налог для книги «личное» не настроен", err)

    def test_no_tax_section_is_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = make_data(Path(tmp) / "d", TOML.split("[tax.")[0] + "[categories]\n")
            code, _, err = self.run_cli(
                "report", "tax", "--book", "бизнес", "--period", "2026-H2", data=data
            )
        self.assertEqual(code, 1)
        self.assertIn("налог для книги «бизнес» не настроен", err)

    def test_period_is_only_for_tax(self):
        code, _, err = self.run_cli("report", "balance", "--period", "2026-H2")
        self.assertEqual(code, 1)
        self.assertIn("--period подходит только к report tax", err)


if __name__ == "__main__":
    unittest.main()
