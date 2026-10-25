import tempfile
import unittest
from pathlib import Path

from ledger.journal import EXPENSE_CATEGORIES, JournalError
from ledger.merchants import categorize, exact_category, load_rules


class MerchantsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "merchants.csv"

    def write(self, body):
        self.path.write_text("pattern,category\n" + body, encoding="utf-8")

    def test_missing_file_means_no_rules(self):
        self.assertEqual(load_rules(self.path, EXPENSE_CATEGORIES), [])

    def test_first_matching_rule_wins_ignoring_case(self):
        self.write("foodie.eda,кафе и доставка\nridely,транспорт\n")
        rules = load_rules(self.path, EXPENSE_CATEGORIES)
        self.assertEqual(categorize("FOODIE.EDA", rules), "кафе и доставка")
        self.assertEqual(categorize("Ridely.Go", rules), "транспорт")

    def test_unknown_merchant_falls_back(self):
        self.write("ridely,транспорт\n")
        self.assertEqual(categorize("Сквер", load_rules(self.path, EXPENSE_CATEGORIES)), "прочее")

    def test_exact_rule_matches_the_whole_description_only(self):
        self.write("=сквер,развлечения\nfoodie.eda,кафе и доставка\n")
        rules = load_rules(self.path, EXPENSE_CATEGORIES)
        self.assertEqual(categorize("  СКВЕР ", rules), "развлечения")
        self.assertEqual(categorize("Скверовка ЦУМ", rules), "прочее")
        self.assertEqual(categorize("Технопарк", rules), "прочее")
        self.assertEqual(exact_category("Сквер", rules), "развлечения")
        self.assertIsNone(exact_category("FOODIE.EDA", rules))

    def test_substring_rule_ignores_extra_whitespace(self):
        self.write("romashka shop,продукты\n")
        self.assertEqual(
            categorize('TOO "ROMASHKA   SHOP"', load_rules(self.path, EXPENSE_CATEGORIES)),
            "продукты",
        )

    def test_unknown_category_is_rejected_with_line(self):
        self.write("ridely,транспорт\nmetro,метро\n")
        with self.assertRaisesRegex(JournalError, "строка 3"):
            load_rules(self.path, EXPENSE_CATEGORIES)


if __name__ == "__main__":
    unittest.main()
