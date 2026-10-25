import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from ledger.datadir import DataDir
from ledger.journal import EXPENSE_CATEGORIES, INCOME_CATEGORIES, LedgerError
from ledger.settings import SCHEMA, SettingsError, is_currency, load_settings
from tests.helpers import MINIMAL, make_data

# Ключи с кириллицей в TOML 1.0 берутся в кавычки: [tax."бизнес"].
FULL = """\
schema = 2
code = "C:/Users/anya/budzheved"

[owner]
currency = "KZT"
country = "KZ"

[books]
default = "личное"
names = ["личное", "бизнес"]

[accounts.kaspi-gold]
book = "личное"
currencies = ["KZT", "USD"]
import = "kaspi"

[accounts.paypal]
book = "бизнес"
currencies = ["USD"]

[people.anya]
name = "Аня"
role = "партнёр"
settlements = true
default_share = 50

[tax."бизнес"]
rate = 3
base = "доходы"
period = "полугодие"
categories = ["продажи"]

[categories]
expense = ["закупка товара", "доставка", "реклама", "прочее"]
income = ["продажи"]
"""

BOOKS = '[books]\ndefault = "личное"\nnames = ["личное", "бизнес"]\n'
PERSON = '[people.anya]\nname = "Аня"\nrole = "партнёр"\n'
TAX = '[tax."бизнес"]\nrate = 3\nbase = "доходы"\nperiod = "полугодие"\n'


def toml(*parts: str, head: str = 'schema = 2\ncode = "."\n') -> str:
    owner = '[owner]\ncurrency = "KZT"\ncountry = "KZ"\n'
    return head + owner + "".join(parts)


class SettingsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def load(self, text: str):
        return load_settings(make_data(self.tmp, text).settings_file)


class LoadTest(SettingsCase):
    def test_full_example(self):
        s = self.load(FULL)
        self.assertEqual(s.schema, 2)
        self.assertEqual(s.code, "C:/Users/anya/budzheved")
        self.assertEqual((s.currency, s.country), ("KZT", "KZ"))
        self.assertEqual(s.books, ("личное", "бизнес"))
        self.assertEqual(s.default_book, "личное")
        self.assertEqual(s.accounts["kaspi-gold"].currencies, ("KZT", "USD"))
        self.assertEqual(s.accounts["kaspi-gold"].importer, "kaspi")
        self.assertEqual(s.accounts["paypal"].book, "бизнес")
        self.assertEqual(s.accounts["paypal"].importer, "")
        self.assertEqual(s.people["anya"].name, "Аня")
        self.assertEqual(s.people["anya"].default_share, 50)
        self.assertTrue(s.people["anya"].settlements)
        self.assertEqual(s.tax["бизнес"].rate, Decimal("3"))
        self.assertEqual(s.tax["бизнес"].categories, ("продажи",))
        self.assertIn("закупка товара", s.expense_categories)
        self.assertIn("продажи", s.income_categories)

    def test_categories_extend_standard_without_repeats(self):
        s = self.load(FULL)
        self.assertEqual(s.expense_categories[: len(EXPENSE_CATEGORIES)], EXPENSE_CATEGORIES)
        self.assertEqual(s.expense_categories.count("прочее"), 1)
        self.assertEqual(s.income_categories, (*INCOME_CATEGORIES, "продажи"))

    def test_minimal(self):
        s = self.load(MINIMAL)
        self.assertEqual(s.books, ("",))
        self.assertEqual(s.default_book, "")
        self.assertEqual(s.accounts, {})
        self.assertEqual(s.people, {})
        self.assertEqual(s.tax, {})
        self.assertEqual(s.expense_categories, EXPENSE_CATEGORIES)
        self.assertEqual(s.income_categories, INCOME_CATEGORIES)
        self.assertFalse(s.has_settlements)
        self.assertFalse(s.has_accounts)
        self.assertFalse(s.has_books)

    def test_has_flags(self):
        s = self.load(FULL)
        self.assertTrue(s.has_settlements)
        self.assertTrue(s.has_accounts)
        self.assertTrue(s.has_books)

    def test_settlement_people_only_with_flag(self):
        extra = '[people.boris]\nname = "Борис"\nrole = "поставщик"\n'
        s = self.load(FULL + extra)
        self.assertEqual([p.key for p in s.settlement_people()], ["anya"])
        self.assertFalse(s.people["boris"].settlements)

    def test_currency_order(self):
        s = self.load(FULL)
        self.assertEqual(
            s.currency_order(["USD", "USDT", "KZT", "RUB"]), ["KZT", "RUB", "USD", "USDT"]
        )

    def test_currency_order_owner_first_and_unique(self):
        s = self.load(MINIMAL)
        self.assertEqual(
            s.currency_order(["USD", "KZT", "USDT", "USD", "RUB"]), ["USDT", "KZT", "RUB", "USD"]
        )

    def test_old_schema_is_readable(self):
        s = self.load(MINIMAL.replace("schema = 2", "schema = 1"))
        self.assertEqual(s.schema, 1)

    def test_default_book_falls_back_to_first_name(self):
        s = self.load(toml('[books]\nnames = ["личное", "бизнес"]\n'))
        self.assertEqual(s.default_book, "личное")

    def test_account_book_defaults_to_default_book(self):
        s = self.load(toml(BOOKS, '[accounts.x]\ncurrencies = ["KZT"]\n'))
        self.assertEqual(s.accounts["x"].book, "личное")

    def test_float_rate(self):
        s = self.load(toml(BOOKS, TAX.replace("rate = 3", "rate = 2.5")))
        self.assertEqual(s.tax["бизнес"].rate, Decimal("2.5"))

    def test_is_currency(self):
        for code in ("KZT", "USD", "USDT", "EUR"):
            self.assertTrue(is_currency(code), code)
        for code in ("kzt", "US", "USDX", "К3Т", "", "USD1"):
            self.assertFalse(is_currency(code), code)

    def test_schema_constant(self):
        self.assertEqual(SCHEMA, 2)

    def test_settings_error_is_ledger_error(self):
        self.assertTrue(issubclass(SettingsError, LedgerError))


class RejectTest(SettingsCase):
    def assertRejects(self, text: str, fragment: str):
        with self.assertRaisesRegex(SettingsError, fragment):
            self.load(text)

    def test_rejections(self):
        cases = {
            "typo in person key": (
                toml(PERSON + "setlements = true\n"),
                r"people\.anya\.setlements",
            ),
            "rate with percent sign": (
                toml(BOOKS, TAX.replace("rate = 3", 'rate = "3%"')),
                r"tax\.бизнес\.rate",
            ),
            "unknown role": (
                toml('[people.anya]\nname = "Аня"\nrole = "друг"\n'),
                "партнёр",
            ),
            "share over 100": (
                toml(PERSON + "default_share = 120\n"),
                "0 до 100",
            ),
            "share below 0": (
                toml(PERSON + "default_share = -1\n"),
                "0 до 100",
            ),
            "account in unknown book": (
                toml(BOOKS, '[accounts.x]\nbook = "дача"\ncurrencies = ["KZT"]\n'),
                r"accounts\.x\.book",
            ),
            "tax for unknown book": (
                toml(BOOKS, TAX.replace("бизнес", "дача")),
                r"tax\.дача",
            ),
            "unknown tax period": (
                toml(BOOKS, TAX.replace("полугодие", "неделя")),
                "полугодие",
            ),
            "unknown tax base": (
                toml(BOOKS, TAX.replace('"доходы"', '"всё"')),
                "доходы-расходы",
            ),
            "backslashes in path": (
                'schema = 2\ncode = "C:\\Users\\x"\n[owner]\ncurrency = "KZT"\ncountry = "KZ"\n',
                "строка.*/",
            ),
            "lowercase currency": (
                toml('[accounts.x]\ncurrencies = ["kzt"]\n'),
                "KZT",
            ),
            "empty currency list": (
                toml("[accounts.x]\ncurrencies = []\n"),
                r"accounts\.x\.currencies",
            ),
            "owner currency lowercase": (
                'schema = 2\ncode = "."\n[owner]\ncurrency = "kzt"\ncountry = "KZ"\n',
                r"owner\.currency",
            ),
            "schema from the future": (
                MINIMAL.replace("schema = 2", "schema = 3"),
                "новее кода",
            ),
            "schema missing": (
                MINIMAL.replace("schema = 2\n", ""),
                r"settings\.toml, schema: обязательный ключ",
            ),
            "schema not a number": (MINIMAL.replace("schema = 2", 'schema = "2"'), "schema"),
            "unknown top-level key": (
                MINIMAL.replace("[owner]", "colour = 1\n[owner]"),
                r"settings\.toml, colour: неизвестный ключ",
            ),
            "unknown owner key": (MINIMAL + "region = 1\n", r"owner\.region"),
            "unknown account key": (
                toml('[accounts.x]\ncurrencies = ["KZT"]\nbank = "b"\n'),
                r"accounts\.x\.bank",
            ),
            "unknown tax key": (
                toml(BOOKS, TAX + "cap = 1\n"),
                r"tax\.бизнес\.cap",
            ),
            "unknown books key": (
                toml('[books]\nnames = ["a"]\nextra = 1\n'),
                r"books\.extra",
            ),
            "unknown categories key": (
                toml('[categories]\nother = ["a"]\n'),
                r"categories\.other",
            ),
            "default book not in names": (
                toml('[books]\ndefault = "дача"\nnames = ["личное"]\n'),
                r"books\.default",
            ),
            "settlements not a bool": (
                toml(PERSON + 'settlements = "yes"\n'),
                r"people\.anya\.settlements",
            ),
            "share not an int": (
                toml(PERSON + 'default_share = "50"\n'),
                r"people\.anya\.default_share",
            ),
            "missing person name": (
                toml('[people.anya]\nrole = "партнёр"\n'),
                r"people\.anya\.name",
            ),
            "category not a string": (
                toml("[categories]\nexpense = [1]\n"),
                r"categories\.expense",
            ),
            "missing owner": (
                'schema = 2\ncode = "."\n',
                r"settings\.toml, owner: обязательная таблица \[owner\]",
            ),
            "bool rate": (
                toml(BOOKS, TAX.replace("rate = 3", "rate = true")),
                r"tax\.бизнес\.rate",
            ),
            "negative rate": (
                toml(BOOKS, TAX.replace("rate = 3", "rate = -3")),
                r"tax\.бизнес\.rate",
            ),
        }
        for name, (text, fragment) in cases.items():
            with self.subTest(name):
                self.assertRejects(text, fragment)

    def test_person_keys_are_lowercase_latin_and_not_me(self):
        for key in ("me", "Anya", "1anya", "an-ya", '"аня"', '"an ya"'):
            with self.subTest(key=key):
                self.assertRejects(
                    toml(f'[people.{key}]\nname = "Аня"\nrole = "партнёр"\n'),
                    r"ключ человека .*: латиница в нижнем регистре, цифры и _, не me",
                )
        self.assertIn("anya_2", self.load(toml(PERSON.replace("anya", "anya_2"))).people)

    def test_cyrillic_bare_key_hint(self):
        with self.assertRaisesRegex(SettingsError, "кавычки"):
            self.load(toml(BOOKS, TAX.replace('"бизнес"', "бизнес")))

    def test_missing_file_points_to_init(self):
        with self.assertRaisesRegex(SettingsError, "init"):
            load_settings(DataDir(self.tmp).settings_file)

    def test_unreadable_encoding(self):
        data = make_data(self.tmp)
        data.settings_file.write_bytes(b"\xff\xfe\x00")
        with self.assertRaisesRegex(SettingsError, "UTF-8"):
            load_settings(data.settings_file)


class DataDirSettingsTest(SettingsCase):
    def test_settings_property_reads_file(self):
        data = make_data(self.tmp, FULL)
        self.assertEqual(data.settings.currency, "KZT")

    def test_settings_are_cached_on_frozen_datadir(self):
        data = make_data(self.tmp, FULL)
        self.assertIs(data.settings, data.settings)

    def test_datadir_stays_frozen_and_comparable(self):
        data = make_data(self.tmp, FULL)
        data.settings  # noqa: B018
        with self.assertRaises(AttributeError):
            data.root = Path("/y")  # type: ignore[misc]
        self.assertEqual(data, DataDir(self.tmp))

    def test_missing_settings_raise_only_on_access(self):
        data = DataDir(self.tmp)
        with self.assertRaisesRegex(SettingsError, "init"):
            data.settings  # noqa: B018


if __name__ == "__main__":
    unittest.main()
