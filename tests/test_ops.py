import multiprocessing
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from ledger import ops
from ledger.calc import book_of
from ledger.datadir import DataDir
from ledger.journal import JournalError, load
from ledger.merchants import load_rules
from tests.helpers import ANYA, MINIMAL, make_data

FIXTURE = Path(__file__).parent / "fixtures" / "kaspi_sample.txt"
DAY = date(2026, 10, 1)


def _add_many(root, worker, count):
    for i in range(count):
        ops.add_entry(
            DataDir(Path(root)),
            date=DAY,
            amount=str(worker * 1000 + i + 1),
            description=f"w{worker}-{i}",
            category="прочее",
            force=True,
        )


class OpsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = make_data(Path(tmp.name) / "Мои деньги", ANYA)
        self.journal = self.data.journal
        self.empty = self.journal.read_bytes()
        self.merchants = self.data.merchants
        self.merchants.write_text(
            "pattern,category\nтакси,транспорт\nkaspi,прочее\n", encoding="utf-8"
        )

    def add(self, **kwargs):
        kwargs.setdefault("date", DAY)
        return ops.add_entry(self.data, **kwargs)

    def test_add_entry_categorises_and_saves(self):
        result = self.add(amount="1 500,50", currency="kzt", description="такси домой")
        self.assertEqual(result.duplicates, [])
        self.assertEqual(
            (result.entry.id, result.entry.amount, result.entry.currency, result.entry.category),
            (1, Decimal("1500.50"), "KZT", "транспорт"),
        )
        self.assertEqual(self.load(), [result.entry])

    def load(self):
        return load(self.journal, self.data.settings)

    def test_add_entry_defaults_to_the_owner_currency(self):
        kzt = make_data(self.data.root.parent / "тенге", MINIMAL.replace('"USDT"', '"KZT"'))
        entry = ops.add_entry(kzt, date=DAY, amount="5", category="прочее").entry
        self.assertEqual(entry.currency, "KZT")

    def test_add_entry_holds_back_a_probable_duplicate(self):
        self.add(amount="12")
        held = self.add(amount="12", date=date(2026, 10, 2))
        self.assertIsNone(held.entry)
        self.assertEqual([e.id for e in held.duplicates], [1])
        self.assertEqual(len(self.load()), 1)
        forced = self.add(amount="12", force=True)
        self.assertEqual(forced.entry.id, 2)

    def test_add_entry_rejects_invalid_data_without_writing(self):
        with self.assertRaises(JournalError):
            self.add(amount="12", currency="euro")
        self.assertEqual(self.journal.read_bytes(), self.empty)

    def test_add_entry_takes_party_and_shares(self):
        result = self.add(amount="300", type="settlement", party="anya", paid_by="anya")
        self.assertEqual((result.entry.party, result.entry.paid_by), ("anya", "anya"))
        shared = self.add(amount="80", shares={"anya": 50})
        self.assertEqual(self.load()[1].shares, {"anya": 50})
        self.assertEqual(shared.entry.shares, {"anya": 50})
        with self.assertRaisesRegex(JournalError, "человек 'boris' не найден"):
            self.add(amount="90", shares={"boris": 50})

    def test_writes_refuse_a_schema_1_folder(self):
        self.add(amount="10")
        old = make_data(
            self.data.root.parent / "старая", MINIMAL.replace("schema = 2", "schema = 1")
        )
        before = old.journal.read_bytes()
        writes = {
            "add": lambda: ops.add_entry(old, date=DAY, amount="5"),
            "shares": lambda: ops.set_shares(old, [1], {}),
            "edit": lambda: ops.edit_entry(old, 1, {"note": "x"}),
            "delete": lambda: ops.delete_entries(old, [1]),
            "merchant": lambda: ops.apply_merchant_category(old, "NOVA", "одежда"),
            "import": lambda: ops.import_statement(old, FIXTURE.read_text(encoding="utf-8")),
        }
        for name, write in writes.items():
            with self.subTest(name), self.assertRaisesRegex(JournalError, "migrate"):
                write()
        self.assertEqual(old.journal.read_bytes(), before)
        self.assertFalse(old.merchants.exists())

    def test_single_person_helpers(self):
        settings = self.data.settings
        self.assertEqual(ops.single_shares(settings, 50), {"anya": 50})
        self.assertEqual(ops.single_shares(settings, 0), {})
        self.assertEqual(ops.default_party(settings, "settlement"), "anya")
        self.assertEqual(ops.default_party(settings, "expense"), "")
        nobody = make_data(self.data.root.parent / "один", MINIMAL).settings
        self.assertEqual(ops.single_shares(nobody, 0), {})
        self.assertEqual(ops.default_party(nobody, "debt"), "")
        with self.assertRaisesRegex(JournalError, "взаиморасчёты выключены"):
            ops.single_shares(nobody, 50)
        two = make_data(
            self.data.root.parent / "двое",
            ANYA + '[people.boris]\nname = "Борис"\nrole = "партнёр"\nsettlements = true\n',
        ).settings
        with self.assertRaisesRegex(JournalError, "укажите, чьи доли: anya:25;boris:25"):
            ops.single_shares(two, 50)
        self.assertEqual(ops.default_party(two, "settlement"), "")

    def test_add_entry_leaves_the_book_to_the_account(self):
        books = MINIMAL + '[books]\nnames = ["личное", "бизнес"]\n'
        data = make_data(
            self.data.root.parent / "счета",
            books + '[accounts.paypal]\nbook = "бизнес"\ncurrencies = ["USD"]\n',
        )
        entry = ops.add_entry(
            data,
            date=DAY,
            account="paypal",
            amount="10",
            currency="USD",
            tax="да",
            receipt="paypal.pdf",
        ).entry
        self.assertEqual(
            (entry.account, entry.book, entry.tax, entry.receipt),
            ("paypal", "", "да", "paypal.pdf"),
        )
        self.assertEqual(book_of(entry, data.settings), "бизнес")
        same = ops.add_entry(
            data, date=DAY, account="paypal", book="бизнес", amount="9", currency="USD"
        ).entry
        self.assertEqual(same.book, "")
        other = ops.add_entry(data, date=DAY, book="бизнес", amount="11").entry
        self.assertEqual((other.account, other.book), ("", "бизнес"))
        with self.assertRaisesRegex(JournalError, "не совпадает с книгой счёта"):
            ops.add_entry(
                data, date=DAY, account="paypal", book="личное", amount="12", currency="USD"
            )
        self.assertEqual(len(load(data.journal, data.settings)), 3)

    def test_moving_an_account_to_another_book_keeps_the_journal_readable(self):
        books = MINIMAL + '[books]\nnames = ["личное", "бизнес"]\n'
        data = make_data(
            self.data.root.parent / "переезд",
            books + '[accounts.paypal]\nbook = "бизнес"\ncurrencies = ["USD"]\n',
        )
        ops.add_entry(data, date=DAY, account="paypal", amount="10", currency="USD")
        data.settings_file.write_text(
            books + '[accounts.paypal]\nbook = "личное"\ncurrencies = ["USD"]\n',
            encoding="utf-8",
        )
        moved = DataDir(data.root)
        (entry,) = load(moved.journal, moved.settings)
        self.assertEqual(book_of(entry, moved.settings), "личное")

    def test_parallel_writers_lose_nothing(self):
        context = multiprocessing.get_context("spawn")
        workers = [
            context.Process(target=_add_many, args=(str(self.data.root), w, 8)) for w in range(4)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(60)
        self.assertEqual([w.exitcode for w in workers], [0, 0, 0, 0])
        entries = self.load()
        self.assertEqual(len(entries), 32)
        self.assertEqual(sorted(e.id for e in entries), list(range(1, 33)))
        self.assertEqual([p.name for p in self.journal.parent.glob("*.tmp")], [])

    def test_ids_are_never_reused_after_deleting_the_last_entry(self):
        for amount in ("1", "2", "3"):
            self.add(amount=amount)
        ops.delete_entries(self.data, [3])
        self.assertEqual(self.add(amount="4").entry.id, 4)
        ops.delete_entries(self.data, [4, 2])
        text = FIXTURE.read_text(encoding="utf-8")
        added = ops.import_statement(self.data, text).added
        self.assertEqual(added[0].id, 5)
        self.assertEqual(self.add(amount="777").entry.id, added[-1].id + 1)

    def test_ids_are_not_reused_in_a_journal_older_than_the_counter(self):
        self.add(amount="1")
        self.add(amount="2")
        self.journal.with_suffix(".seq").unlink()  # журнал, начатый до появления счётчика
        ops.delete_entries(self.data, [2])
        self.assertEqual(self.add(amount="3").entry.id, 3)

    def test_oversized_input_is_refused_and_journal_stays_readable(self):
        self.add(amount="1")
        for bad in (
            {"amount": "5", "description": "a" * 600},
            {"amount": "5", "note": "n" * 600},
            {"amount": "1e-200000"},
            {"amount": "0.123456789"},
        ):
            with self.subTest(bad=str(bad)[:40]):
                with self.assertRaises(JournalError):
                    self.add(force=True, **bad)
        with self.assertRaises(JournalError):
            ops.edit_entry(self.data, 1, {"description": "b" * 600})
        self.assertEqual(len(self.load()), 1)

    def test_merchant_rule_failure_leaves_journal_untouched(self):
        self.add(amount="10", description="Сквер")
        before = self.journal.read_bytes()
        self.merchants.write_text("pattern,category\nпарк,нет такой\n", encoding="utf-8")
        with self.assertRaises(JournalError):
            ops.apply_merchant_category(self.data, "Сквер", "развлечения")
        self.assertEqual(self.journal.read_bytes(), before)

    def test_source_fields_cannot_be_edited(self):
        self.add(amount="10")
        for field in ("source", "source_ref", "id"):
            with self.subTest(field=field):
                with self.assertRaises(JournalError):
                    ops.edit_entry(self.data, 1, {field: "kaspi-pdf"})

    def test_set_shares_and_their_refusals(self):
        self.add(amount="10")
        self.add(amount="20", type="income")
        (changed,) = ops.set_shares(self.data, [1], {"anya": 50})
        self.assertEqual(changed.shares, {"anya": 50})
        with self.assertRaisesRegex(JournalError, "не расходы: 2"):
            ops.set_shares(self.data, [1, 2], {"anya": 100})
        with self.assertRaises(ops.NotFound):
            ops.set_shares(self.data, [7], {"anya": 50})
        with self.assertRaisesRegex(JournalError, "человек 'boris' не найден"):
            ops.set_shares(self.data, [1], {"boris": 50})
        self.assertEqual([e.shares for e in self.load()], [{"anya": 50}, {}])

    def test_edit_entry(self):
        self.add(amount="10", description="кофе")
        updated = ops.edit_entry(
            self.data, 1, {"category": "кафе и доставка", "amount": "1 250,5", "shares": "anya:50"}
        )
        self.assertEqual(
            (updated.category, updated.amount, updated.shares),
            ("кафе и доставка", Decimal("1250.5"), {"anya": 50}),
        )
        for changes in (
            {"category": "метро"},
            {"id": "9"},
            {"colour": "red"},
            {"share": "50"},
            {"shares": "много"},
            {"shares": "boris:50"},
        ):
            with self.subTest(changes=changes), self.assertRaises(JournalError):
                ops.edit_entry(self.data, 1, changes)
        with self.assertRaises(ops.NotFound):
            ops.edit_entry(self.data, 5, {"note": "x"})
        self.assertEqual(self.load(), [updated])

    def test_delete_entries(self):
        for amount in ("10", "20", "30"):
            self.add(amount=amount)
        removed = ops.delete_entries(self.data, [1, 3])
        self.assertEqual([e.id for e in removed], [1, 3])
        self.assertEqual([e.id for e in self.load()], [2])
        with self.assertRaises(ops.NotFound):
            ops.delete_entries(self.data, [2, 9])
        self.assertEqual(len(self.load()), 1)

    def test_merchant_category_updates_same_merchant_and_remembers_rule(self):
        self.add(amount="500", currency="KZT", description='TOO "ROMASHKA SHOP"')
        self.add(amount="995", currency="KZT", description='too  "romashka shop"')
        self.add(amount="700", currency="KZT", description="KASPI TRAVEL")
        self.add(amount="900", currency="KZT", type="income", description='TOO "ROMASHKA SHOP"')
        changed = ops.apply_merchant_category(self.data, 'TOO "ROMASHKA SHOP"', "продукты")
        self.assertEqual([e.id for e in changed], [1, 2])
        self.assertEqual(
            [e.category for e in self.load()],
            ["продукты", "продукты", "прочее", "доход"],
        )
        self.assertEqual(
            load_rules(self.merchants, self.data.settings.expense_categories),
            [('=too "romashka shop"', "продукты"), ("такси", "транспорт"), ("kaspi", "прочее")],
        )
        next_one = self.add(amount="300", currency="KZT", description='TOO "ROMASHKA SHOP"')
        self.assertEqual(next_one.entry.category, "продукты")

    def test_remembered_rule_does_not_shadow_other_merchants(self):
        self.add(amount="500", currency="KZT", description="Kaspi")
        ops.apply_merchant_category(self.data, "Kaspi", "одежда")
        self.add(amount="600", currency="KZT", description="Такси Kaspi")
        self.add(amount="700", currency="KZT", description="KASPI TRAVEL")
        self.add(amount="800", currency="KZT", description="kaspi")
        self.assertEqual(
            [e.category for e in self.load()], ["одежда", "транспорт", "прочее", "одежда"]
        )

    def test_merchant_category_replaces_its_own_earlier_rule(self):
        self.add(amount="500", currency="KZT", description="NOVA")
        ops.apply_merchant_category(self.data, "NOVA", "одежда")
        ops.apply_merchant_category(self.data, "NOVA", "продукты")
        self.assertEqual(
            load_rules(self.merchants, self.data.settings.expense_categories)[0],
            ("=nova", "продукты"),
        )
        self.assertEqual(len(load_rules(self.merchants, self.data.settings.expense_categories)), 3)

    def test_merchant_category_refuses_bad_input(self):
        self.add(amount="500", currency="KZT", description="NOVA")
        before = self.merchants.read_text(encoding="utf-8")
        for description, category in (("  ", "продукты"), ("NOVA", "доход")):
            with self.subTest(description=description), self.assertRaises(JournalError):
                ops.apply_merchant_category(self.data, description, category)
        self.assertEqual(self.merchants.read_text(encoding="utf-8"), before)

    def test_import_statement_dry_run_then_real(self):
        text = FIXTURE.read_text(encoding="utf-8")
        self.add(amount="1000", currency="KZT", description="кофе Lumen")
        preview = ops.import_statement(self.data, text, dry_run=True)
        self.assertEqual((len(preview.added), preview.saved, preview.problems), (9, False, []))
        self.assertEqual([(new.id, twin.id) for new, twin in preview.typed], [(4, 1)])
        self.assertEqual(len(self.load()), 1)
        done = ops.import_statement(self.data, text)
        self.assertTrue(done.saved)
        self.assertEqual(len(self.load()), 10)
        again = ops.import_statement(self.data, text)
        self.assertEqual((again.added, again.saved), ([], False))

    def test_same_statement_on_two_accounts_is_imported_twice(self):
        text = FIXTURE.read_text(encoding="utf-8")
        data = make_data(
            self.data.root.parent / "две карты",
            MINIMAL.replace('"USDT"', '"KZT"')
            + '[accounts.card]\ncurrencies = ["KZT"]\n[accounts.spare]\ncurrencies = ["KZT"]\n',
        )
        self.assertEqual(len(ops.import_statement(data, text, account="card").added), 9)
        spare = ops.import_statement(data, text, account="spare")
        self.assertEqual((len(spare.added), spare.missing), (9, []))
        self.assertEqual(ops.import_statement(data, text, account="card").added, [])
        self.assertEqual(ops.import_statement(data, text, account="spare").added, [])
        self.assertEqual(len(load(data.journal, data.settings)), 18)

    def test_statement_rows_without_account_still_match(self):
        # Записи из журнала без счетов (например, после migrate) уже покрывают выписку.
        text = FIXTURE.read_text(encoding="utf-8")
        plain = MINIMAL.replace('"USDT"', '"KZT"')
        data = make_data(self.data.root.parent / "без счетов", plain)
        self.assertEqual(len(ops.import_statement(data, text).added), 9)
        data.settings_file.write_text(
            plain + '[accounts.card]\ncurrencies = ["KZT"]\n', encoding="utf-8"
        )
        card = DataDir(data.root)
        again = ops.import_statement(card, text, account="card")
        self.assertEqual((again.added, again.missing), ([], []))

    def test_import_statement_that_does_not_reconcile(self):
        text = FIXTURE.read_text(encoding="utf-8").replace(
            "- 494,00 ₸                Разное", "- 400,00 ₸                Разное"
        )
        result = ops.import_statement(self.data, text)
        self.assertTrue(result.problems)
        self.assertFalse(result.saved)
        self.assertEqual(self.journal.read_bytes(), self.empty)


if __name__ == "__main__":
    unittest.main()
