import unittest
from datetime import date
from decimal import Decimal

from ledger.calc import (
    balance_effect,
    balances,
    balances_by_currency,
    book_of,
    category_groups,
    category_totals,
    debt_effect,
    debts,
    established_currencies,
    my_amount,
    opening_pairs,
)
from ledger.journal import Entry
from tests.helpers import make_settings

D = Decimal

# Два человека, два счёта в двух книгах; валюта владельца KZT.
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

[accounts.cash]
currencies = ["KZT"]

[accounts.paypal]
book = "бизнес"
currencies = ["USD"]

[people.anya]
name = "Аня"
role = "партнёр"
settlements = true

[people.boris]
name = "Борис"
role = "партнёр"
settlements = true

[categories]
expense = ["сырьё"]
income = ["продажи"]
"""
S = make_settings(TOML)


def entry(**overrides):
    base = dict(id=1, date=date(2026, 10, 1), type="expense", amount=D("100"), currency="KZT")
    base.update(overrides)
    return Entry(**base)


class EffectTableTest(unittest.TestCase):
    """Каждая строка таблицы из раздела 5 спеки, с одним человеком anya."""

    def check(self, e, balance, mine, debt):
        self.assertEqual(balance_effect(e), balance)
        self.assertEqual(my_amount(e), mine)
        self.assertEqual({k: v for k, v in debt_effect(e).items() if v}, debt)

    def test_expense_paid_by_me(self):
        kzt = {("", "KZT"): D("-100")}
        self.check(entry(), kzt, D("100"), {})
        self.check(entry(shares={"anya": 50}), kzt, D("50"), {("anya", "KZT"): D("50")})
        self.check(entry(shares={"anya": 100}), kzt, D("0"), {("anya", "KZT"): D("100")})

    def test_expense_is_split_between_several_people(self):
        e = entry(shares={"anya": 25, "boris": 25})
        self.assertEqual(debt_effect(e), {("anya", "KZT"): D("25"), ("boris", "KZT"): D("25")})
        self.assertEqual(my_amount(e), D("50"))

    def test_expense_paid_by_anya(self):
        self.check(
            entry(paid_by="anya", shares={"anya": 50}), {}, D("50"), {("anya", "KZT"): D("-50")}
        )
        self.check(entry(paid_by="anya"), {}, D("100"), {("anya", "KZT"): D("-100")})

    def test_expense_paid_by_boris_with_his_share(self):
        e = entry(paid_by="boris", shares={"boris": 50})
        self.assertEqual(debt_effect(e), {("boris", "KZT"): D("-50")})

    def test_refund_reverses_expense(self):
        refund = entry(amount=D("-21.70"), shares={"anya": 50})
        self.check(refund, {("", "KZT"): D("21.70")}, D("-10.850"), {("anya", "KZT"): D("-10.850")})

    def test_income(self):
        self.check(entry(type="income"), {("", "KZT"): D("100")}, D("100"), {})

    def test_exchange(self):
        e = entry(type="exchange", amount=D("45000"), amount2=D("100"), currency2="USDT")
        self.check(e, {("", "KZT"): D("-45000"), ("", "USDT"): D("100")}, D("0"), {})

    def test_exchange_between_accounts(self):
        e = entry(
            type="exchange",
            account="card",
            account2="paypal",
            amount=D("45000"),
            amount2=D("100"),
            currency2="USD",
        )
        self.assertEqual(
            balance_effect(e), {("card", "KZT"): D("-45000"), ("paypal", "USD"): D("100")}
        )
        within = entry(
            type="exchange", account="card", amount=D("45000"), amount2=D("100"), currency2="USD"
        )
        self.assertEqual(
            balance_effect(within), {("card", "KZT"): D("-45000"), ("card", "USD"): D("100")}
        )

    def test_cash_withdrawal(self):
        self.check(entry(type="cash_withdrawal"), {}, D("0"), {})
        e = entry(type="cash_withdrawal", account="card", account2="cash")
        self.assertEqual(balance_effect(e), {("card", "KZT"): D("-100"), ("cash", "KZT"): D("100")})

    def test_cash_withdrawal_from_an_account_moves_money_to_no_account(self):
        # Так пишет импорт Kaspi: счёт выписки есть, второго счёта нет.
        e = entry(type="cash_withdrawal", account="card")
        self.check(e, {("card", "KZT"): D("-100"), ("", "KZT"): D("100")}, D("0"), {})
        opening = entry(id=2, type="opening_balance", account="card", amount=D("1000"))
        self.assertEqual(balances([opening, e]), {("card", "KZT"): D("900"), ("", "KZT"): D("100")})
        self.assertEqual(balances_by_currency([opening, e]), {"KZT": D("1000")})

    def test_transfer_moves_money_between_accounts(self):
        e = entry(
            type="transfer", account="card", account2="paypal", amount=D("100"), currency="USD"
        )
        self.check(e, {("card", "USD"): D("-100"), ("paypal", "USD"): D("100")}, D("0"), {})

    def test_settlement(self):
        self.check(
            entry(type="settlement", party="anya"),
            {("", "KZT"): D("-100")},
            D("0"),
            {("anya", "KZT"): D("100")},
        )
        self.check(
            entry(type="settlement", paid_by="anya", party="anya"),
            {("", "KZT"): D("100")},
            D("0"),
            {("anya", "KZT"): D("-100")},
        )

    def test_settlement_keeps_debts_of_two_people_apart(self):
        entries = [
            entry(id=1, shares={"anya": 50, "boris": 50}),
            entry(id=2, type="settlement", amount=D("30"), paid_by="anya", party="anya"),
            entry(id=3, type="settlement", amount=D("20"), party="boris"),
        ]
        self.assertEqual(debts(entries), {("anya", "KZT"): D("20"), ("boris", "KZT"): D("70")})

    def test_debt_conversion(self):
        mine = entry(
            type="debt_conversion",
            party="anya",
            amount=D("179711"),
            amount2=D("400"),
            currency2="USDT",
        )
        self.check(mine, {}, D("0"), {("anya", "KZT"): D("-179711"), ("anya", "USDT"): D("400")})
        anyas = entry(
            type="debt_conversion",
            paid_by="anya",
            party="anya",
            amount=D("179711"),
            amount2=D("400"),
            currency2="USDT",
        )
        self.check(anyas, {}, D("0"), {("anya", "KZT"): D("179711"), ("anya", "USDT"): D("-400")})

    def test_openings(self):
        self.check(entry(type="opening_balance"), {("", "KZT"): D("100")}, D("0"), {})
        self.check(
            entry(type="opening_balance", account="card"), {("card", "KZT"): D("100")}, D("0"), {}
        )

    def test_debt_follows_the_creditor(self):
        self.check(entry(type="debt", party="anya"), {}, D("0"), {("anya", "KZT"): D("100")})
        self.check(
            entry(type="debt", paid_by="anya", party="anya"),
            {},
            D("0"),
            {("anya", "KZT"): D("-100")},
        )


class TotalsTest(unittest.TestCase):
    def setUp(self):
        self.entries = [
            entry(id=1, type="opening_balance", amount=D("1000"), currency="USDT"),
            entry(
                id=2, type="debt", paid_by="anya", party="anya", amount=D("850"), currency="USDT"
            ),
            entry(id=3, amount=D("6421.31"), shares={"anya": 50}, category="развлечения"),
            entry(
                id=4,
                amount=D("8000"),
                paid_by="anya",
                shares={"anya": 50},
                category="кафе и доставка",
            ),
            entry(id=5, type="income", amount=D("50000"), category="входящие переводы"),
            entry(id=6, type="settlement", party="anya", amount=D("300"), currency="USDT"),
            entry(id=7, date=date(2026, 9, 30), amount=D("700"), category="развлечения"),
        ]

    def test_balances_per_account_and_currency(self):
        self.assertEqual(
            balances(self.entries),
            {("", "USDT"): D("700"), ("", "KZT"): D("50000") - D("6421.31") - D("700")},
        )

    def test_balances_by_currency_add_up_the_accounts(self):
        entries = [
            entry(id=1, type="opening_balance", account="card", amount=D("1000")),
            entry(id=2, type="opening_balance", account="cash", amount=D("500")),
            entry(id=3, type="opening_balance", amount=D("50")),
            entry(id=4, type="transfer", account="card", account2="cash", amount=D("200")),
        ]
        self.assertEqual(
            balances(entries),
            {("card", "KZT"): D("800"), ("cash", "KZT"): D("700"), ("", "KZT"): D("50")},
        )
        self.assertEqual(balances_by_currency(entries), {"KZT": D("1550")})

    def test_debts_per_person_and_currency_without_intermediate_rounding(self):
        self.assertEqual(
            debts(self.entries),
            {("anya", "USDT"): D("-550"), ("anya", "KZT"): D("3210.655") - D("4000")},
        )

    def test_opening_pairs_and_established_currencies(self):
        self.assertEqual(opening_pairs(self.entries), {("", "USDT")})
        self.assertEqual(established_currencies(self.entries), {"USDT"})

    def test_category_totals_use_my_share_and_one_month(self):
        self.assertEqual(
            category_totals(self.entries, S, 2026, 10),
            {
                ("expense", "KZT", "развлечения"): D("3210.655"),
                ("expense", "KZT", "кафе и доставка"): D("4000"),
                ("income", "KZT", "входящие переводы"): D("50000"),
            },
        )


class BookTest(unittest.TestCase):
    def test_book_of_entry_then_account_then_default(self):
        self.assertEqual(book_of(entry(book="бизнес"), S), "бизнес")
        self.assertEqual(book_of(entry(account="paypal"), S), "бизнес")
        self.assertEqual(book_of(entry(account="card"), S), "личное")
        self.assertEqual(book_of(entry(), S), "личное")
        # книга записи важнее книги счёта
        self.assertEqual(book_of(entry(book="личное", account="paypal"), S), "личное")

    def test_transfer_is_not_an_expense_or_income(self):
        e = entry(type="transfer", book="бизнес", account="card", account2="paypal", currency="USD")
        self.assertEqual(category_totals([e], S, 2026, 10), {})
        self.assertEqual(category_totals([e], S, 2026, 10, book="бизнес"), {})
        self.assertEqual(category_totals([e], S, 2026, 10, book="личное"), {})
        self.assertEqual(balances([e]), {("card", "USD"): D("-100"), ("paypal", "USD"): D("100")})

    def test_category_totals_take_only_the_asked_book(self):
        entries = self.entries()
        self.assertEqual(
            category_totals(entries, S, 2026, 10, book="бизнес"),
            {("expense", "USD", "сырьё"): D("40"), ("income", "USD", "продажи"): D("90")},
        )
        self.assertEqual(
            category_totals(entries, S, 2026, 10, book="личное"),
            {("expense", "KZT", "продукты"): D("300")},
        )
        self.assertEqual(len(category_totals(entries, S, 2026, 10)), 3)

    def entries(self):
        return [
            entry(id=1, amount=D("300"), category="продукты", account="card"),
            entry(id=2, amount=D("40"), currency="USD", category="сырьё", account="paypal"),
            entry(
                id=3,
                type="income",
                amount=D("90"),
                currency="USD",
                category="продажи",
                book="бизнес",
            ),
        ]

    def test_category_groups_follow_settings_orders_and_the_book(self):
        entries = self.entries() + [
            entry(id=4, amount=D("5"), category="прочее"),
            entry(id=5, amount=D("7"), category="продукты", currency="USD", account="card"),
        ]
        groups = category_groups(entries, S, 2026, 10)
        # валюта владельца (KZT) первая, категории как в настройках
        self.assertEqual(
            groups["expense"],
            [
                ("KZT", [("продукты", D("300")), ("прочее", D("5"))], D("305")),
                ("USD", [("продукты", D("7")), ("сырьё", D("40"))], D("47")),
            ],
        )
        self.assertEqual(
            category_groups(entries, S, 2026, 10, book="бизнес"),
            {
                "expense": [("USD", [("сырьё", D("40"))], D("40"))],
                "income": [("USD", [("продажи", D("90"))], D("90"))],
            },
        )


if __name__ == "__main__":
    unittest.main()
