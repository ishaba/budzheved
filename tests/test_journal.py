import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from ledger.journal import (
    FIELDS,
    FIELDS_V1,
    Entry,
    JournalError,
    LedgerError,
    atomic_writer,
    find_duplicates,
    format_shares,
    from_row,
    load,
    next_id,
    parse_amount,
    parse_month,
    parse_shares,
    require_writable,
    save,
    to_row,
    v1_share_column,
    validate,
)
from tests.helpers import ANYA, MINIMAL, make_settings

# Двое с расчётами, работник без расчётов, два счёта в двух книгах.
PEOPLE = """\
schema = 2
code = "."

[owner]
currency = "KZT"
country = "KZ"

[books]
names = ["личное", "бизнес"]

[accounts.card]
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

[people.cleaner]
name = "Уборщица"
role = "работник"
"""
S = make_settings(PEOPLE)
SINGLE = make_settings(ANYA)


def entry(**overrides):
    base = dict(
        id=1,
        date=date(2026, 10, 1),
        type="expense",
        amount=Decimal("100"),
        currency="KZT",
        category="прочее",
    )
    base.update(overrides)
    return Entry(**base)


class ParseAmountTest(unittest.TestCase):
    def test_accepts_human_formats(self):
        self.assertEqual(parse_amount("1500"), Decimal("1500"))
        self.assertEqual(parse_amount("1 500,50"), Decimal("1500.50"))
        self.assertEqual(parse_amount("1 500,5"), Decimal("1500.5"))
        self.assertEqual(parse_amount("+2000"), Decimal("2000"))
        self.assertEqual(parse_amount("-21.70"), Decimal("-21.70"))

    def test_rejects_non_numbers(self):
        for text in ("abc", "", "NaN", "Infinity", "1,500.50"):
            with self.subTest(text=text), self.assertRaises(JournalError):
                parse_amount(text)


class SharesTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_shares("anya:25;boris:25"), {"anya": 25, "boris": 25})
        self.assertEqual(parse_shares(" anya : 50 "), {"anya": 50})
        self.assertEqual(parse_shares(""), {})

    def test_sum_over_100_is_refused(self):
        with self.assertRaisesRegex(JournalError, "сумма долей 110 больше 100"):
            parse_shares("anya:60;boris:50")

    def test_malformed_shares_are_refused(self):
        for text in ("anya", "anya:", ":50", "anya:half", "anya:-5", "anya:5;anya:5", "anya:1.5"):
            with self.subTest(text=text), self.assertRaises(JournalError):
                parse_shares(text)

    def test_format(self):
        self.assertEqual(format_shares({"anya": 25, "boris": 25}), "anya:25;boris:25")
        self.assertEqual(format_shares({}), "")


class ValidateTest(unittest.TestCase):
    def test_plain_expense_is_valid(self):
        self.assertEqual(validate(entry(), S), [])

    def test_refund_is_negative_expense(self):
        self.assertEqual(validate(entry(amount=Decimal("-21.70")), S), [])

    def test_two_sided_types_are_valid_with_second_leg(self):
        exchange = entry(type="exchange", category="", amount2=Decimal("450"), currency2="USDT")
        self.assertEqual(validate(exchange, S), [])

    def test_schema_2_entries_are_valid(self):
        valid = {
            "shares of two": entry(shares={"anya": 25, "boris": 25}),
            "paid by a person with own share": entry(paid_by="anya", shares={"anya": 50}),
            "party on an expense": entry(party="cleaner"),
            "settlement": entry(type="settlement", category="", party="anya", paid_by="anya"),
            "debt": entry(type="debt", category="", party="boris"),
            "debt conversion": entry(
                type="debt_conversion",
                category="",
                party="anya",
                paid_by="anya",
                amount2=Decimal("1"),
                currency2="USD",
            ),
            "account and its book": entry(account="paypal", currency="USD", book="бизнес"),
            "account without book": entry(account="card"),
            "transfer": entry(
                type="transfer", category="", account="card", account2="card2", currency="KZT"
            ),
            "cash withdrawal from an account": entry(
                type="cash_withdrawal", category="", account="card"
            ),
            "cash withdrawal between accounts": entry(
                type="cash_withdrawal", category="", account="card", account2="card2"
            ),
            "tax override": entry(tax="нет"),
            "receipt": entry(receipt="2026/10/check.jpg"),
            "import source": entry(source="import:halyk"),
            "receipt source": entry(source="receipt"),
            "any ISO currency": entry(currency="EUR"),
        }
        settings = make_settings(PEOPLE + '[accounts.card2]\ncurrencies = ["KZT"]\n')
        for name, good in valid.items():
            with self.subTest(name):
                self.assertEqual(validate(good, settings), [])

    def test_each_rule_reports_its_own_problem(self):
        invalid = {
            "неизвестный тип 'gift'": entry(type="gift"),
            "неизвестная валюта 'EURO'": entry(currency="EURO"),
            "человек 'petya' не найден в settings.toml": entry(paid_by="petya"),
            "неизвестный источник 'email'": entry(source="email"),
            "неизвестный источник 'import:'": entry(source="import:"),
            "неизвестный источник 'import:a.b'": entry(source="import:a.b"),
            "неизвестный источник 'import:../x'": entry(source="import:../x"),
            "неизвестный источник 'import:Halyk'": entry(source="import:Halyk"),
            "сумма расхода не может быть нулевой": entry(amount=Decimal("0")),
            "неизвестная категория расхода 'доход'": entry(category="доход"),
            "сумма должна быть положительной": entry(
                type="income", category="доход", amount=Decimal("-5")
            ),
            "неизвестная категория дохода 'прочее'": entry(type="income", category="прочее"),
            "доход всегда записывается на меня": entry(
                type="income", category="доход", paid_by="anya"
            ),
            "категория допустима только у расхода и дохода": entry(type="settlement", party="anya"),
            "нужна положительная вторая сумма": entry(type="exchange", category=""),
            "валюты двух сторон должны различаться": entry(
                type="exchange", category="", amount2=Decimal("1"), currency2="KZT"
            ),
            "вторая сумма допустима только у обмена и пересчёта долга": entry(
                amount2=Decimal("1"), currency2="USD"
            ),
        }
        for problem, bad in invalid.items():
            with self.subTest(problem):
                self.assertIn(problem, validate(bad, S))
        huge = {
            "первая сумма": entry(amount=Decimal("1e30")),
            "вторая сумма": entry(
                type="exchange", category="", amount2=Decimal("1e15"), currency2="USDT"
            ),
        }
        for name, bad in huge.items():
            with self.subTest(name):
                self.assertEqual(validate(bad, S), ["сумма слишком велика"])

    def test_schema_2_rules(self):
        """По строке на каждое правило схемы 2: запись и кусок текста ошибки."""
        transfer = dict(type="transfer", category="", account="card")
        invalid = {
            "shares on income": (
                entry(type="income", category="доход", shares={"anya": 50}),
                "доли людей допустимы только у расхода",
            ),
            "share of unknown person": (
                entry(shares={"vasya": 50}),
                "человек 'vasya' не найден в settings.toml",
            ),
            "share of person without settlements": (
                entry(shares={"cleaner": 50}),
                "у человека 'cleaner' нет взаиморасчётов",
            ),
            "shares over 100": (
                entry(shares={"anya": 60, "boris": 50}),
                "сумма долей 110 больше 100",
            ),
            "share out of range": (entry(shares={"anya": -5}), "доля 'anya'"),
            "payer without settlements": (
                entry(paid_by="cleaner"),
                "у человека 'cleaner' нет взаиморасчётов",
            ),
            "payer with other people's shares": (
                entry(paid_by="anya", shares={"boris": 50}),
                "платил 'anya', а доли есть у других: boris",
            ),
            "unknown party": (entry(party="vasya"), "человек 'vasya' не найден в settings.toml"),
            "settlement without party": (
                entry(type="settlement", category=""),
                "у settlement нужен человек в party",
            ),
            "debt with party without settlements": (
                entry(type="debt", category="", party="cleaner"),
                "у человека 'cleaner' нет взаиморасчётов",
            ),
            "debt conversion without party": (
                entry(type="debt_conversion", category="", amount2=Decimal("1"), currency2="USD"),
                "у debt_conversion нужен человек в party",
            ),
            "debt creditor is a third person": (
                entry(type="debt", category="", party="anya", paid_by="boris"),
                "paid_by 'boris': у debt это me или 'anya'",
            ),
            "debt conversion creditor is a third person": (
                entry(
                    type="debt_conversion",
                    category="",
                    party="anya",
                    paid_by="boris",
                    amount2=Decimal("1"),
                    currency2="USD",
                ),
                "paid_by 'boris': у debt_conversion это me или 'anya'",
            ),
            "unknown account": (entry(account="kaspi"), "счёт 'kaspi' не найден в settings.toml"),
            "unknown second account": (
                entry(
                    type="exchange",
                    category="",
                    account="card",
                    account2="kaspi",
                    amount2=Decimal("1"),
                    currency2="USD",
                ),
                "счёт 'kaspi' не найден в settings.toml",
            ),
            "currency not of the account": (
                entry(account="paypal", currency="KZT"),
                "валюты KZT нет у счёта 'paypal'",
            ),
            "second currency not of the second account": (
                entry(
                    type="exchange",
                    category="",
                    account="card",
                    account2="card",
                    amount2=Decimal("1"),
                    currency2="USD",
                ),
                "валюты USD нет у счёта 'card'",
            ),
            "second currency falls back to the first account": (
                entry(
                    type="exchange",
                    category="",
                    account="card",
                    amount2=Decimal("1"),
                    currency2="USD",
                ),
                "валюты USD нет у счёта 'card'",
            ),
            "transfer without second account": (
                entry(**transfer),
                "у перевода между счетами нужны оба счёта",
            ),
            "transfer to the same account": (
                entry(**{**transfer, "account2": "card"}),
                "счета перевода должны различаться",
            ),
            "transfer with second amount": (
                entry(
                    **{**transfer, "account2": "paypal", "currency": "USD"},
                    amount2=Decimal("1"),
                    currency2="KZT",
                ),
                "вторая сумма допустима только у обмена и пересчёта долга",
            ),
            "cash withdrawal to the same account": (
                entry(type="cash_withdrawal", category="", account="card", account2="card"),
                "счета снятия наличных должны различаться",
            ),
            "cash withdrawal to an account without the currency": (
                entry(type="cash_withdrawal", category="", account="card", account2="paypal"),
                "валюты KZT нет у счёта 'paypal'",
            ),
            "transfer with category": (
                entry(**{**transfer, "account2": "paypal", "category": "прочее"}),
                "категория допустима только у расхода и дохода",
            ),
            "unknown book": (entry(book="дача"), "книга 'дача' не найдена в settings.toml"),
            "book differs from account's book": (
                entry(book="личное", account="paypal", currency="USD"),
                "книга 'личное' не совпадает с книгой счёта 'paypal' ('бизнес')",
            ),
            "unknown tax mark": (entry(tax="может"), "налог: да, нет или пусто, а не 'может'"),
            "tax mark on a settlement": (
                entry(type="settlement", category="", party="anya", tax="да"),
                "отметка налога допустима только у дохода и расхода",
            ),
            "absolute receipt": (entry(receipt="/etc/passwd"), "путь к чеку"),
            "windows absolute receipt": (entry(receipt="C:\\check.jpg"), "путь к чеку"),
            "receipt outside receipts": (entry(receipt="../journal.csv"), "путь к чеку"),
            "receipt with backslash parent": (entry(receipt="a\\..\\..\\x"), "путь к чеку"),
            "custom category from other settings": (
                entry(category="аренда"),
                "неизвестная категория расхода 'аренда'",
            ),
        }
        for name, (bad, fragment) in invalid.items():
            with self.subTest(name):
                self.assertIn(fragment, "; ".join(validate(bad, S)))

    def test_only_expenses_and_people_types_have_another_payer(self):
        bad = {
            "exchange": entry(type="exchange", category="", amount2=Decimal("1"), currency2="USD"),
            "transfer": entry(type="transfer", category="", account="card", account2="card2"),
            "cash_withdrawal": entry(type="cash_withdrawal", category=""),
            "opening_balance": entry(type="opening_balance", category=""),
        }
        settings = make_settings(PEOPLE + '[accounts.card2]\ncurrencies = ["KZT"]\n')
        for kind, good in bad.items():
            with self.subTest(kind):
                self.assertEqual(validate(good, settings), [])
                good.paid_by = "anya"
                self.assertIn(f"paid_by 'anya': у {kind} это всегда me", validate(good, settings))

    def test_accounts_need_accounts_table(self):
        self.assertIn(
            "счёт 'card' не найден в settings.toml", validate(entry(account="card"), SINGLE)
        )

    def test_books_need_books_table(self):
        self.assertIn(
            "книга 'личное' не найдена в settings.toml", validate(entry(book="личное"), SINGLE)
        )

    def test_custom_categories_come_from_settings(self):
        custom = make_settings(MINIMAL + '[categories]\nexpense = ["аренда"]\n')
        self.assertEqual(validate(entry(category="аренда"), custom), [])

    def test_text_and_precision_limits(self):
        cases = {
            "описание": (entry(description="a" * 501), "описание"),
            "примечание": (entry(note="a" * 501), "примечание"),
            "перенос строки": (entry(description="a\nb"), "перенос"),
            "дробь": (entry(amount=Decimal("1.123456789")), "знаков"),
            "мелкая экспонента": (entry(amount=Decimal("1e-200000")), "знаков"),
        }
        for name, (bad, fragment) in cases.items():
            with self.subTest(name):
                self.assertIn(fragment, "; ".join(validate(bad, S)))
        good = entry(description="a" * 500, amount=Decimal("1.12345678"))
        self.assertEqual(validate(good, S), [])

    def test_parse_amount_accepts_only_plain_numbers(self):
        self.assertEqual(parse_amount("1 500,50"), Decimal("1500.50"))
        self.assertEqual(parse_amount("-21.7"), Decimal("-21.7"))
        self.assertEqual(parse_amount("+5"), Decimal("5"))
        for bad in ("1_000", "１２", "1e3", "", "1.2.3", "abc", ".5", "5."):
            with self.subTest(bad=bad):
                with self.assertRaises(JournalError):
                    parse_amount(bad)


class PartyMessagesTest(unittest.TestCase):
    def test_missing_party_does_not_repeat_itself_as_a_payer_problem(self):
        problems = validate(entry(type="settlement", category="", paid_by="anya"), S)
        self.assertEqual(problems, ["у settlement нужен человек в party"])

    def test_payer_other_than_the_party_is_still_named(self):
        problems = validate(entry(type="debt", category="", party="anya", paid_by="boris"), S)
        self.assertEqual(problems, ["paid_by 'boris': у debt это me или 'anya'"])


class RowTest(unittest.TestCase):
    def test_round_trip_with_cyrillic_and_every_field(self):
        full = entry(
            book="бизнес",
            account="paypal",
            currency="USD",
            description="Кофе «Шоколадница», 2 шт.",
            paid_by="anya",
            party="anya",
            shares={"anya": 25},
            tax="да",
            receipt="2026/10/чек.jpg",
            note="примечание",
        )
        row = to_row(full)
        self.assertEqual(list(row), list(FIELDS))
        self.assertEqual(row["shares"], "anya:25")
        self.assertEqual(from_row(row), full)

    def test_field_order(self):
        self.assertEqual(
            FIELDS,
            (
                "id", "date", "type", "book", "account", "amount", "currency", "account2",
                "amount2", "currency2", "category", "description", "paid_by", "party", "shares",
                "tax", "source", "source_ref", "receipt", "note",
            ),
        )  # fmt: skip
        self.assertEqual(v1_share_column(FIELDS_V1), FIELDS_V1[10])
        self.assertTrue(FIELDS_V1[10].endswith("_share"))
        self.assertNotIn("share", FIELDS)


class StorageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "data" / "journal.csv"

    def test_missing_and_empty_files_are_empty_journals(self):
        self.assertEqual(load(self.path, S), [])
        self.path.parent.mkdir()
        self.path.write_text("", encoding="utf-8")
        self.assertEqual(load(self.path, S), [])

    def test_round_trip_keeps_commas_quotes_and_second_leg(self):
        entries = [
            entry(description='ТОО "ROMASHKA SHOP", 12 mkr', note="149,50 TRY"),
            entry(
                id=2,
                type="exchange",
                category="",
                amount=Decimal("45000.50"),
                amount2=Decimal("100"),
                currency2="USDT",
            ),
            entry(id=3, shares={"anya": 25, "boris": 25}, account="card", book="личное"),
        ]
        save(self.path, entries)
        self.assertEqual(load(self.path, S), entries)

    def test_save_leaves_no_temporary_file(self):
        save(self.path, [entry()])
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["journal.csv"])

    def test_unexpected_header_is_rejected(self):
        self.path.parent.mkdir()
        self.path.write_text("id,date\n1,2026-10-01\n", encoding="utf-8")
        with self.assertRaisesRegex(JournalError, "заголовок"):
            load(self.path, S)

    def test_schema_1_header_points_to_migrate(self):
        self.path.parent.mkdir()
        self.path.write_text(
            ",".join(FIELDS_V1) + "\n1,2026-10-01,expense,100,KZT,,,прочее,,me,0,text,,\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            JournalError, "журнал схемы 1: выполните budget.py migrate --person <ключ>"
        ):
            load(self.path, S)

    def test_schema_1_share_column_is_any_key_share(self):
        self.path.parent.mkdir()
        row = "\n1,2026-10-01,expense,100,KZT,,,прочее,,me,0,text,,\n"
        for column in ("anya_share", "boris_share", "b2_share"):
            header = list(FIELDS_V1)
            header[10] = column
            self.path.write_text(",".join(header) + row, encoding="utf-8")
            with self.subTest(column=column), self.assertRaisesRegex(JournalError, "migrate"):
                load(self.path, S)
        for column in ("share", "Anya_share", "anya-share", "anya_shares", "_share", "2_share"):
            header = list(FIELDS_V1)
            header[10] = column
            self.path.write_text(",".join(header) + row, encoding="utf-8")
            with (
                self.subTest(column=column),
                self.assertRaisesRegex(JournalError, "неожиданный заголовок"),
            ):
                load(self.path, S)

    def test_errors_name_the_line_and_the_entry(self):
        save(self.path, [entry(), entry(id=1)])
        with self.assertRaisesRegex(JournalError, r"строка 3 \(запись #1\): повтор id 1"):
            load(self.path, S)
        save(self.path, [entry(), entry(id=2, currency="EURO")])
        with self.assertRaisesRegex(JournalError, r"строка 3 \(запись #2\): неизвестная валюта"):
            load(self.path, S)

    def test_person_removed_from_settings_fails_load(self):
        save(self.path, [entry(id=11), entry(id=12, shares={"anya": 50})])
        without_anya = make_settings(PEOPLE.replace("[people.anya]", "[people.vera]"))
        with self.assertRaises(JournalError) as caught:
            load(self.path, without_anya)
        message = str(caught.exception)
        self.assertIn("запись #12", message)
        self.assertIn("человек 'anya' не найден в settings.toml", message)

    def test_account_removed_from_settings_fails_load(self):
        save(self.path, [entry(id=5, account="card")])
        with self.assertRaisesRegex(
            JournalError, r"запись #5\): счёт 'card' не найден в settings.toml"
        ):
            load(self.path, SINGLE)

    def test_unreadable_value_names_the_line(self):
        save(self.path, [entry()])
        text = self.path.read_text(encoding="utf-8").replace("2026-10-01", "вчера")
        self.path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(JournalError, "строка 2"):
            load(self.path, S)

    def test_unreadable_shares_name_the_line(self):
        save(self.path, [entry(shares={"anya": 50})])
        text = self.path.read_text(encoding="utf-8").replace("anya:50", "anya:half")
        self.path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(JournalError, r"строка 2 \(запись #1\): .*anya:half"):
            load(self.path, S)

    def test_unreadable_row_keeps_the_record_number(self):
        save(self.path, [entry(id=7, shares={"anya": 50})])
        text = self.path.read_text(encoding="utf-8").replace("anya:50", "anya:half")
        self.path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(JournalError, r"строка 2 \(запись #7\): .*anya:half"):
            load(self.path, S)

    def test_unreadable_row_with_a_broken_id_has_no_record_number(self):
        save(self.path, [entry(id=7)])
        text = self.path.read_text(encoding="utf-8").replace("\n7,", "\nseven,")
        self.path.write_text(text, encoding="utf-8")
        with self.assertRaises(JournalError) as caught:
            load(self.path, S)
        self.assertIn("строка 2: ", str(caught.exception))
        self.assertNotIn("запись #", str(caught.exception))

    def test_rows_with_wrong_column_count_are_rejected(self):
        save(self.path, [entry()])
        good = self.path.read_text(encoding="utf-8")
        for name, broken in (
            ("лишние", good.rstrip("\r\n") + ",x,y\r\n"),
            ("недостающие", good.rstrip("\r\n").rsplit(",", 1)[0] + "\r\n"),
        ):
            with self.subTest(name):
                self.path.write_text(broken, encoding="utf-8")
                with self.assertRaisesRegex(JournalError, "строка 2: .*колон"):
                    load(self.path, S)

    def test_unparsable_csv_is_a_journal_error(self):
        save(self.path, [entry()])
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(
                "2,2026-10-01,expense,,,1,KZT,,,,прочее," + "a" * 140000 + ",me,,,,text,,,\r\n"
            )
        with self.assertRaises(JournalError):
            load(self.path, S)


class ReplaceRetryTest(unittest.TestCase):
    """На Windows антивирус ненадолго держит файл: замена повторяется, потом сдаётся."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "journal.csv"
        self.path.write_text("старое", encoding="utf-8")
        self.real = os.replace

    def write(self):
        with atomic_writer(self.path) as fh:
            fh.write("новое")

    def test_busy_file_is_replaced_after_a_short_wait(self):
        failures = [PermissionError(13, "занято")] * 2

        def busy_twice(src, dst):
            if failures:
                raise failures.pop()
            self.real(src, dst)

        with (
            mock.patch("ledger.journal.os.replace", side_effect=busy_twice) as replace,
            mock.patch("ledger.journal.time.sleep") as sleep,
        ):
            self.write()
        self.assertEqual(replace.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "новое")

    def test_file_busy_for_too_long_is_an_error_and_leaves_no_temporary(self):
        with (
            mock.patch("ledger.journal.os.replace", side_effect=PermissionError(13, "занято")),
            mock.patch("ledger.journal.time.sleep") as sleep,
            self.assertRaises(PermissionError),
        ):
            self.write()
        self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 1.0)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "старое")
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["journal.csv"])


class WritableTest(unittest.TestCase):
    def test_schema_1_settings_refuse_writes(self):
        old = make_settings(MINIMAL.replace("schema = 2", "schema = 1"))
        with self.assertRaisesRegex(JournalError, "журнал схемы 1: выполните budget.py migrate"):
            require_writable(old)

    def test_current_schema_is_writable(self):
        require_writable(S)


class HelpersTest(unittest.TestCase):
    def test_next_id(self):
        self.assertEqual(next_id([]), 1)
        self.assertEqual(next_id([entry(id=4), entry(id=9)]), 10)

    def test_duplicates_match_amount_currency_and_adjacent_day(self):
        existing = [
            entry(id=1, date=date(2026, 9, 30)),
            entry(id=2, date=date(2026, 9, 28)),
            entry(id=3, currency="RUB"),
            entry(id=4, amount=Decimal("101")),
        ]
        found = find_duplicates(existing, entry(id=5))
        self.assertEqual([e.id for e in found], [1])


class MonthAndErrorsTest(unittest.TestCase):
    def test_parse_month(self):
        self.assertEqual(parse_month("2026-10"), (2026, 10))
        for bad in ("2026", "2026-13", "окт", "2026-10-01", ""):
            with self.subTest(bad):
                with self.assertRaisesRegex(JournalError, "нужен месяц в виде ГГГГ-ММ"):
                    parse_month(bad)

    def test_every_expected_failure_shares_one_base(self):
        from ledger.kaspi import KaspiError
        from ledger.ops import NotFound

        for error in (JournalError, KaspiError, NotFound):
            self.assertTrue(issubclass(error, LedgerError), error)


if __name__ == "__main__":
    unittest.main()
