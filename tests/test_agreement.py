"""Страница и команды report показывают одни и те же цифры."""

import unittest
from datetime import date
from decimal import Decimal

from ledger import calc, reports, view
from ledger.journal import Entry
from tests.helpers import ANYA, make_settings

D = Decimal
S = make_settings()


def journal():
    rows = [
        dict(type="opening_balance", amount=D("1000"), currency="USDT", category=""),
        dict(
            type="debt",
            amount=D("850"),
            currency="USDT",
            category="",
            paid_by="anya",
            party="anya",
        ),
        dict(type="expense", amount=D("1500.50"), currency="KZT", category="кафе и доставка"),
        dict(
            type="expense",
            amount=D("8001"),
            currency="KZT",
            category="продукты",
            shares={"anya": 50},
        ),
        dict(
            type="expense",
            amount=D("33.33"),
            currency="USDT",
            category="транспорт",
            paid_by="anya",
            shares={"anya": 50},
        ),
        dict(type="expense", amount=D("-200"), currency="KZT", category="продукты"),
        dict(type="income", amount=D("2000"), currency="USDT", category="доход"),
        dict(type="income", amount=D("5000"), currency="KZT", category="входящие переводы"),
        dict(type="settlement", amount=D("300"), currency="USDT", category="", party="anya"),
        dict(
            type="exchange",
            amount=D("100"),
            currency="USDT",
            category="",
            amount2=D("45000"),
            currency2="KZT",
        ),
        dict(
            type="expense",
            amount=D("70"),
            currency="RUB",
            category="прочее",
            date=date(2026, 9, 30),
        ),
    ]
    return [Entry(**{"id": i, "date": date(2026, 10, 1), **row}) for i, row in enumerate(rows, 1)]


class AgreementTest(unittest.TestCase):
    def setUp(self):
        self.entries = journal()
        self.state = view.state(self.entries, S, (2026, 10))

    def test_balances(self):
        report = reports.balance_report(self.entries, S)
        self.assertEqual(len(self.state["balances"]), 3)
        for tile in self.state["balances"]:
            self.assertIn(f"  {tile['currency']}: {tile['text']}", report)

    def test_debts(self):
        report = reports.debt_report(self.entries, S).splitlines()
        words = {"i_owe": "мой долг", "they_owe": "долг мне"}
        self.assertEqual(len(self.state["debts"]), 2)
        for debt in self.state["debts"]:
            self.assertEqual((debt["person"], debt["name"]), ("anya", "Аня"))
            self.assertEqual(
                debt["line"],
                f"{debt['name']}, {debt['currency']}: {words[debt['direction']]} {debt['text']}",
            )
            self.assertIn(f"  {debt['line']}", report)

    def test_debts_of_two_people(self):
        s = make_settings(
            ANYA + '[people.boris]\nname = "Борис"\nrole = "партнёр"\nsettlements = true\n'
        )
        entries = self.entries + [
            Entry(
                id=20,
                date=date(2026, 10, 2),
                type="expense",
                amount=D("1000"),
                currency="KZT",
                category="продукты",
                shares={"anya": 30, "boris": 30},
            ),
            Entry(
                id=21,
                date=date(2026, 10, 2),
                type="debt",
                amount=D("40"),
                currency="USD",
                paid_by="boris",
                party="boris",
            ),
        ]
        report = reports.debt_report(entries, s).splitlines()
        lines = [f"  {debt['line']}" for debt in view.state(entries, s)["debts"]]
        self.assertEqual(lines, report[1 : len(lines) + 1])
        self.assertIn("  Борис, USD: мой долг 40,00", lines)
        self.assertIn("  Борис, KZT: долг мне 300,00", lines)

    def test_currencies_follow_the_settings_order(self):
        lines = reports.balance_report(self.entries, S).splitlines()[1:]
        self.assertEqual(
            [line.split(":")[0].strip() for line in lines],
            [tile["currency"] for tile in self.state["balances"]],
        )
        self.assertEqual(self.state["options"]["currencies"], ["USDT", "KZT", "RUB", "USD"])

    def test_categories(self):
        report = reports.categories_report(self.entries, S, 2026, 10)
        seen = 0
        for groups in self.state["categories"].values():
            for group in groups:
                self.assertIn(f"    итого: {group['total_text']}", report)
                for row in group["rows"]:
                    seen += 1
                    self.assertIn(f"    {row['category']}: {row['text']}", report)
        self.assertEqual(seen, 5)
        self.assertNotIn("RUB", report)

    def test_category_groups_follow_the_fixed_order(self):
        groups = calc.category_groups(self.entries, S, 2026, 10)
        self.assertEqual(
            groups["expense"],
            [
                ("USDT", [("транспорт", D("16.665"))], D("16.665")),
                (
                    "KZT",
                    [("продукты", D("3800.5")), ("кафе и доставка", D("1500.50"))],
                    D("5301.0"),
                ),
            ],
        )
        self.assertEqual([currency for currency, _, _ in groups["income"]], ["USDT", "KZT"])
        self.assertEqual(
            calc.category_groups(self.entries, S, 2026, 8), {"expense": [], "income": []}
        )
