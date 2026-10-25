import unittest
from datetime import date
from decimal import Decimal

from ledger import reports, view
from ledger.journal import Entry
from tests.helpers import ANYA, MINIMAL, make_settings

D = Decimal
S = make_settings()
TWO = ANYA + '[people.boris]\nname = "Борис"\nrole = "партнёр"\nsettlements = true\n'


def entry(**overrides):
    base = dict(
        id=1,
        date=date(2026, 10, 1),
        type="expense",
        amount=D("100"),
        currency="KZT",
        category="прочее",
    )
    base.update(overrides)
    return Entry(**base)


class ViewTest(unittest.TestCase):
    def setUp(self):
        self.entries = [
            entry(
                id=1,
                date=date(2026, 9, 23),
                type="debt",
                category="",
                paid_by="anya",
                party="anya",
                amount=D("850"),
                currency="USDT",
            ),
            entry(
                id=2,
                date=date(2026, 9, 23),
                type="opening_balance",
                category="",
                amount=D("1000"),
                currency="USDT",
            ),
            entry(id=3, date=date(2026, 9, 30), amount=D("700"), category="продукты"),
            entry(
                id=4,
                amount=D("6421.31"),
                shares={"anya": 50},
                category="развлечения",
                description="playbox",
                note="149,50 TRY",
            ),
            entry(id=5, type="income", amount=D("50000"), category="входящие переводы"),
        ]
        self.state = view.state(self.entries, S)

    def test_entries_are_newest_first_with_display_fields(self):
        self.assertEqual([e["id"] for e in self.state["entries"]], [5, 4, 3, 2, 1])
        row = self.state["entries"][1]
        self.assertEqual(
            (row["amount"], row["amount_text"], row["share"], row["type_label"], row["date"]),
            ("6421.31", "6 421,31", 50, "расход", "2026-10-01"),
        )
        self.assertEqual(row["shares"], "anya:50")
        self.assertIn("(доли: Аня 50%)", row["line"])
        self.assertIn("playbox", row["line"])

    def test_month_defaults_to_latest_with_entries(self):
        self.assertEqual(self.state["month"], "2026-10")
        self.assertEqual(self.state["months"], ["2026-10", "2026-09"])
        self.assertEqual(self.state["first_date"], "2026-09-23")

    def test_balances_mark_missing_opening(self):
        self.assertEqual(
            self.state["balances"],
            [
                {"currency": "USDT", "has_opening": True, "text": "1 000,00"},
                {"currency": "KZT", "has_opening": False, "text": "+42 878,69"},
            ],
        )

    def test_debts_have_direction_and_positive_text(self):
        self.assertEqual(
            self.state["debts"],
            [
                {
                    "person": "anya",
                    "name": "Аня",
                    "currency": "USDT",
                    "direction": "i_owe",
                    "text": "850,00",
                    "line": "Аня, USDT: мой долг 850,00",
                },
                {
                    "person": "anya",
                    "name": "Аня",
                    "currency": "KZT",
                    "direction": "they_owe",
                    "text": "3 210,66",
                    "line": "Аня, KZT: долг мне 3 210,66",
                },
            ],
        )

    def test_categories_for_selected_month(self):
        self.assertEqual(
            self.state["categories"]["expense"],
            [
                {
                    "currency": "KZT",
                    "total_text": "3 210,66",
                    "rows": [{"category": "развлечения", "text": "3 210,66"}],
                }
            ],
        )
        september = view.state(self.entries, S, (2026, 9))
        self.assertEqual(september["month"], "2026-09")
        self.assertEqual(
            september["categories"]["expense"][0]["rows"],
            [{"category": "продукты", "text": "700,00"}],
        )
        self.assertEqual(september["categories"]["income"], [])

    def test_options_list_everything_the_form_needs(self):
        options = self.state["options"]
        self.assertEqual(options["currencies"][0], "USDT")
        self.assertIn({"value": "settlement", "label": "расчёт"}, options["types"])
        self.assertIn({"value": "debt", "label": "долг"}, options["types"])
        self.assertNotIn("transfer", [t["value"] for t in options["types"]])
        self.assertEqual(options["shares"], [0, 50, 100])
        self.assertEqual(
            options["payers"], [{"value": "me", "label": "я"}, {"value": "anya", "label": "Аня"}]
        )
        alone = view.state([], make_settings(MINIMAL))["options"]
        self.assertEqual(alone["payers"], [{"value": "me", "label": "я"}])
        self.assertIn("продукты", options["expense_categories"])

    def test_share_person_is_the_only_settlement_person(self):
        self.assertEqual(self.state["share_person"], {"key": "anya", "name": "Аня"})
        self.assertIsNone(view.state([], make_settings(MINIMAL))["share_person"])
        self.assertIsNone(view.state([], make_settings(TWO))["share_person"])

    def test_share_person_ignores_people_without_settlements(self):
        s = make_settings(
            ANYA + '[people.boris]\nname = "Борис"\nrole = "поставщик"\nsettlements = false\n'
        )
        self.assertEqual(view.state([], s)["share_person"], {"key": "anya", "name": "Аня"})

    def test_only_types_with_a_page_form_are_editable(self):
        accounts = make_settings(
            ANYA + '[accounts.card]\ncurrencies = ["KZT"]\n[accounts.cash]\ncurrencies = ["KZT"]\n'
        )
        transfer = entry(type="transfer", category="", account="card", account2="cash")
        self.assertFalse(view.entry_dict(transfer, accounts)["editable"])
        self.assertTrue(all(row["editable"] for row in self.state["entries"]))

    def test_party_types_only_with_one_settlement_person(self):
        # Человека для расчёта и долга страница не выбирает: она берёт единственного.
        for toml in (MINIMAL, TWO):
            types = [t["value"] for t in view.state([], make_settings(toml))["options"]["types"]]
            self.assertEqual(
                types, ["expense", "income", "exchange", "cash_withdrawal", "opening_balance"]
            )

    def test_currencies_start_with_the_owner_currency(self):
        kzt = make_settings(MINIMAL.replace('"USDT"', '"KZT"'))
        self.assertEqual(
            view.state([], kzt)["options"]["currencies"], ["KZT", "RUB", "USD", "USDT"]
        )

    def test_balance_tiles_sum_the_accounts(self):
        s = make_settings(
            MINIMAL
            + '[accounts.card]\ncurrencies = ["KZT"]\n[accounts.cash]\ncurrencies = ["KZT"]\n'
        )
        entries = [
            entry(id=1, type="income", account="card", amount=D("500"), category="доход"),
            entry(id=2, account="cash", amount=D("120")),
        ]
        self.assertEqual(
            view.state(entries, s)["balances"],
            [{"currency": "KZT", "has_opening": False, "text": "+380,00"}],
        )

    def test_opening_on_one_account_does_not_establish_the_currency(self):
        s = make_settings(
            MINIMAL
            + '[accounts.card]\ncurrencies = ["KZT"]\n[accounts.cash]\ncurrencies = ["KZT"]\n'
        )
        opening = entry(id=1, type="opening_balance", account="card", amount=D("1000"), category="")
        entries = [opening, entry(id=2, account="cash", amount=D("100"))]
        self.assertEqual(
            view.state(entries, s)["balances"],
            [{"currency": "KZT", "has_opening": False, "text": "+900,00"}],
        )
        text = reports.balance_report(entries, s)
        self.assertIn("card, KZT: 1 000,00", text)
        self.assertIn("cash, KZT: -100,00 (без стартового остатка", text)
        cash_opening = entry(
            id=3, type="opening_balance", account="cash", amount=D("50"), category=""
        )
        self.assertEqual(
            view.state([*entries, cash_opening], s)["balances"],
            [{"currency": "KZT", "has_opening": True, "text": "950,00"}],
        )

    def test_entry_shares_of_two_people_are_a_string(self):
        two = make_settings(TWO)
        row = view.entry_dict(entry(shares={"anya": 50, "boris": 25}), two)
        self.assertEqual((row["shares"], row["share"]), ("anya:50;boris:25", 75))

    def test_empty_journal(self):
        empty = view.state([], S)
        self.assertEqual(
            (empty["entries"], empty["balances"], empty["debts"], empty["months"]), ([], [], [], [])
        )
        self.assertEqual(empty["month"], empty["today"][:7])


if __name__ == "__main__":
    unittest.main()
