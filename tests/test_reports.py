import unittest
from datetime import date
from decimal import Decimal

from ledger.journal import Entry
from ledger.reports import (
    balance_report,
    categories_report,
    debt_report,
    entry_line,
    format_money,
    format_signed,
)
from tests.helpers import ANYA, MINIMAL, make_settings

D = Decimal
S = make_settings(ANYA)
TWO = make_settings(ANYA + '[people.boris]\nname = "Борис"\nrole = "партнёр"\nsettlements = true\n')
# Две книги и два счёта; валюта владельца KZT.
BOOKS = make_settings(
    MINIMAL.replace('currency = "USDT"', 'currency = "KZT"')
    + '[books]\ndefault = "личное"\nnames = ["личное", "бизнес"]\n'
    + '[accounts.card]\nbook = "личное"\ncurrencies = ["KZT"]\n'
    + '[accounts.paypal]\nbook = "бизнес"\ncurrencies = ["USD", "KZT"]\n'
)


def entry(**overrides):
    base = dict(id=1, date=date(2026, 10, 1), type="expense", amount=D("100"), currency="KZT")
    base.update(overrides)
    return Entry(**base)


class FormatTest(unittest.TestCase):
    def test_money(self):
        self.assertEqual(format_money(D("163992.41")), "163 992,41")
        self.assertEqual(format_money(D("3210.655")), "3 210,66")
        self.assertEqual(format_money(D("-1000")), "-1 000,00")
        self.assertEqual(format_money(D("-0.001")), "0,00")

    def test_signed(self):
        self.assertEqual(format_signed(D("5")), "+5,00")
        self.assertEqual(format_signed(D("-5")), "-5,00")

    def test_entry_line_shows_everything_needed_to_recognise_the_entry(self):
        line = entry_line(
            entry(
                id=7,
                amount=D("6421.31"),
                category="развлечения",
                description="playbox",
                shares={"anya": 50},
                note="149,50 TRY",
            ),
            S,
        )
        self.assertEqual(
            line,
            "#7 2026-10-01 расход 6 421,31 KZT [развлечения] playbox (доли: Аня 50%) (149,50 TRY)",
        )

    def test_entry_line_names_people_from_settings(self):
        line = entry_line(entry(shares={"anya": 25, "boris": 25}), TWO)
        self.assertIn("(доли: Аня 25%, Борис 25%)", line)
        settlement = entry(type="settlement", party="boris", paid_by="boris")
        self.assertIn("расчёт 100,00 KZT (с Борис) (платил Борис)", entry_line(settlement, TWO))

    def test_entry_line_names_payer_and_creditor(self):
        self.assertIn("(платил Аня)", entry_line(entry(paid_by="anya"), S))
        debt = entry(type="debt", party="anya", paid_by="anya")
        self.assertIn("долг 100,00 KZT (с Аня) (кредитор Аня)", entry_line(debt, S))
        self.assertIn("(кредитор я)", entry_line(entry(type="debt", party="anya"), S))
        self.assertIn(
            "→ 100,00 USDT",
            entry_line(entry(type="exchange", amount2=D("100"), currency2="USDT"), S),
        )

    def test_entry_line_names_accounts(self):
        self.assertIn("100,00 KZT (счёт card)", entry_line(entry(account="card"), BOOKS))
        transfer = entry(type="transfer", account="card", account2="paypal")
        self.assertIn(
            "перевод между счетами 100,00 KZT (счета card → paypal)", entry_line(transfer, BOOKS)
        )
        self.assertNotIn("счёт", entry_line(entry(), S))


class ReportsTest(unittest.TestCase):
    def setUp(self):
        self.entries = [
            entry(id=1, type="opening_balance", amount=D("1000"), currency="USDT"),
            entry(
                id=2, type="debt", party="anya", paid_by="anya", amount=D("850"), currency="USDT"
            ),
            entry(id=3, amount=D("6421.31"), shares={"anya": 50}, category="развлечения"),
            entry(id=4, type="income", amount=D("50000"), category="входящие переводы"),
            entry(id=5, amount=D("3000"), category="продукты"),
        ]

    def test_balance_marks_currency_without_opening(self):
        self.assertEqual(
            balance_report(self.entries, S),
            "Остатки:\n"
            "  USDT: 1 000,00\n"
            "  KZT: +40 578,69 (без стартового остатка, изменение с начала журнала)",
        )

    def test_balance_of_empty_journal(self):
        self.assertEqual(balance_report([], S), "Остатки:\n  записей нет")

    def test_balance_per_account_in_settings_order_without_account_last(self):
        entries = [
            entry(id=1, type="income", amount=D("5"), currency="KZT", category="доход"),
            entry(id=2, type="opening_balance", account="paypal", amount=D("10"), currency="USD"),
            entry(id=3, type="opening_balance", account="card", amount=D("700")),
            entry(id=4, type="income", account="paypal", amount=D("3"), category="доход"),
        ]
        self.assertEqual(
            balance_report(entries, BOOKS),
            "Остатки:\n"
            "  card, KZT: 700,00\n"
            "  paypal, KZT: +3,00 (без стартового остатка, изменение с начала журнала)\n"
            "  paypal, USD: 10,00\n"
            "  без счёта, KZT: +5,00 (без стартового остатка, изменение с начала журнала)",
        )

    def test_categories(self):
        self.assertEqual(
            categories_report(self.entries, S, 2026, 10),
            "Расходы за 2026-10:\n"
            "  KZT\n"
            "    продукты: 3 000,00\n"
            "    развлечения: 3 210,66\n"
            "    итого: 6 210,66\n"
            "Доходы за 2026-10:\n"
            "  KZT\n"
            "    входящие переводы: 50 000,00\n"
            "    итого: 50 000,00",
        )

    def test_categories_of_one_book(self):
        entries = [
            entry(id=1, amount=D("100"), category="продукты"),
            entry(id=2, account="paypal", amount=D("40"), category="услуги"),
            entry(id=3, book="бизнес", amount=D("2"), category="связь и подписки"),
        ]
        self.assertEqual(
            categories_report(entries, BOOKS, 2026, 10, book="бизнес"),
            "Расходы за 2026-10, книга бизнес:\n"
            "  KZT\n"
            "    связь и подписки: 2,00\n"
            "    услуги: 40,00\n"
            "    итого: 42,00\n"
            "Доходы за 2026-10, книга бизнес:\n"
            "  нет записей",
        )
        self.assertIn("продукты: 100,00", categories_report(entries, BOOKS, 2026, 10))

    def test_categories_of_empty_month(self):
        self.assertEqual(
            categories_report(self.entries, S, 2026, 11),
            "Расходы за 2026-11:\n  нет записей\nДоходы за 2026-11:\n  нет записей",
        )

    def test_debt(self):
        report = debt_report(self.entries, S)
        self.assertIn("  Аня, USDT: мой долг 850,00", report)
        self.assertIn("  Аня, KZT: долг мне 3 210,66", report)
        self.assertIn("#2 2026-10-01 долг", report)
        self.assertIn("Операции (плюс: долг мне, минус: мой долг):", report)
        self.assertIn("| долг +3 210,66 KZT", report)
        self.assertNotIn("#5 ", report)

    def test_debt_when_settled(self):
        self.assertEqual(debt_report([entry()], S), "Долг:\n  мы в расчёте")

    def test_debt_per_person(self):
        entries = [
            entry(id=1, shares={"anya": 25, "boris": 25}),
            entry(id=2, type="debt", party="boris", paid_by="boris", amount=D("10")),
            entry(id=3, type="settlement", party="anya", paid_by="anya", amount=D("25")),
        ]
        report = debt_report(entries, TWO)
        self.assertTrue(report.startswith("Долг:\n  Борис, KZT: долг мне 15,00\nОперации"), report)
        self.assertIn("| долг Аня +25,00 KZT, Борис +25,00 KZT", report)
        self.assertIn("| долг Аня -25,00 KZT", report)

    def test_owner_currency_first_then_alphabetical(self):
        entries = [
            entry(id=1, type="income", amount=D("5"), currency="EUR", category="доход"),
            entry(id=2, type="income", amount=D("5"), currency="AED", category="доход"),
            entry(id=3, type="income", amount=D("5"), currency="KZT", category="доход"),
        ]
        lines = balance_report(entries, S).splitlines()
        self.assertEqual([line.split(":")[0].strip() for line in lines[1:]], ["AED", "EUR", "KZT"])
        lines = balance_report(entries, BOOKS).splitlines()
        self.assertEqual(
            [line.split(":")[0].strip() for line in lines[1:]],
            ["без счёта, KZT", "без счёта, AED", "без счёта, EUR"],
        )


if __name__ == "__main__":
    unittest.main()
