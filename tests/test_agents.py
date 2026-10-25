import contextlib
import io
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ledger import agents, cli
from ledger.journal import EXPENSE_CATEGORIES, INCOME_CATEGORIES, LedgerError
from tests.helpers import ANYA, MINIMAL, make_data, make_settings

# Всё включено: книги, счета, налог, человек с расчётами.
FULL = (
    MINIMAL.replace('currency = "USDT"', 'currency = "KZT"')
    + '[books]\ndefault = "личное"\nnames = ["личное", "бизнес"]\n'
    + '[accounts.card]\ncurrencies = ["KZT", "USD"]\nimport = "kaspi"\n'
    + '[accounts.shop]\nbook = "бизнес"\ncurrencies = ["USD"]\n'
    + '[people.anya]\nname = "Аня"\nrole = "партнёр"\nsettlements = true\ndefault_share = 50\n'
    + '[tax."бизнес"]\nrate = 3\nbase = "доходы"\nperiod = "полугодие"\n'
    + 'categories = ["продажи"]\n'
    + '[categories]\nincome = ["продажи"]\n'
)
TWO = ANYA + '[people.boris]\nname = "Борис"\nrole = "партнёр"\nsettlements = true\n'
SHARES_VALUE = re.compile(r"--shares\s+(\S)")
PLACEHOLDER = re.compile(r"\{(run|people|accounts|currency)\}")
TOML_BLOCK = re.compile(r"```toml\n(.*?)```", re.S)


def render(toml: str, platform: str = "darwin", **flags) -> str:
    return agents.render_core(make_settings(toml), platform, **flags)


class RenderTest(unittest.TestCase):
    def test_block_has_markers_and_no_template_leftovers(self):
        for toml in (MINIMAL, ANYA, FULL):
            for flags in ({}, {"in_git": True, "has_extensions": True}):
                with self.subTest(toml=toml[-30:], **flags):
                    text = render(toml, **flags)
                    self.assertTrue(text.startswith(agents.BEGIN + "\n"))
                    self.assertTrue(text.endswith(agents.END + "\n"))
                    self.assertNotIn("<!-- if", text)
                    self.assertNotIn("<!-- endif", text)
                    self.assertIsNone(PLACEHOLDER.search(text))

    def test_without_people_no_settlements(self):
        text = render(MINIMAL)
        for word in ("--shares", "долг", "paid-by", "доля по умолчанию", "report debt"):
            self.assertNotIn(word, text)

    def test_names_only_people_from_settings(self):
        without = render(MINIMAL)
        for word in ("Аня", "anya", "Борис", "boris"):
            self.assertNotIn(word, without)
        with_anya = render(ANYA)
        self.assertIn("Аня (anya), доля по умолчанию 50%", with_anya)
        self.assertIn("--shares", with_anya)
        self.assertIn("report debt", with_anya)
        self.assertNotIn("Борис", with_anya)
        self.assertNotIn("boris", with_anya)

    def test_shares_examples_are_quoted(self):
        text = render(TWO)
        self.assertIn("Борис (boris)", text)
        starts = SHARES_VALUE.findall(text)
        self.assertGreaterEqual(len(starts), 5)
        self.assertEqual(set(starts), {'"'})
        self.assertIn('--shares "<ключ>:25;<ключ>:25"', text)
        self.assertIn("в кавычки", text)

    def test_main_rule_names_share_only_with_settlements(self):
        def main_rule(text):
            return text.split("## Главное правило")[1].split("##")[0]

        self.assertIn("`add`, `edit`, `delete`, `share` и импорт", main_rule(render(ANYA)))
        self.assertIn("`add`, `edit`, `delete` и импорт", main_rule(render(MINIMAL)))
        self.assertNotIn("share", main_rule(render(MINIMAL)))

    def test_run_command_by_platform(self):
        windows = render(ANYA, "win32")
        self.assertIn("py budget.py add", windows)
        self.assertNotIn("python3 budget.py", windows)
        self.assertIn("python3 budget.py add", render(ANYA, "darwin"))
        self.assertIn("python3 budget.py add", render(ANYA, "linux"))

    def test_tax_only_when_configured(self):
        self.assertNotIn("report tax", render(ANYA))
        self.assertIn("report tax --book", render(FULL))

    def test_accounts_and_books_only_when_configured(self):
        plain = render(ANYA)
        for word in ("--account", "transfer", "--book"):
            self.assertNotIn(word, plain)
        full = render(FULL)
        self.assertIn("`card`: KZT, USD, книга «личное», импорт kaspi", full)
        self.assertIn("`shop`: USD, книга «бизнес»", full)
        self.assertIn("--type transfer", full)

    def test_currency_and_backups(self):
        self.assertIn("Валюта по умолчанию KZT", render(FULL))
        self.assertIn("restore", render(MINIMAL, in_git=False))
        self.assertNotIn("restore", render(MINIMAL, in_git=True))

    def test_update_and_excel_export_are_named(self):
        text = render(MINIMAL)
        self.assertIn("python3 budget.py update", text)
        self.assertIn("python3 budget.py export --excel", text)

    @unittest.skipUnless(shutil.which("git"), "нужен git")
    def test_write_knows_whether_the_folder_is_in_git(self):
        self.assertFalse(hasattr(agents, "_in_git"))
        with tempfile.TemporaryDirectory() as tmp:
            data = make_data(Path(tmp) / "Мои деньги", MINIMAL)
            agents.write(data)
            self.assertIn("restore", (data.root / "AGENTS.md").read_text(encoding="utf-8"))
            for command in (["init", "-q"], ["add", "journal.csv"]):
                subprocess.run(["git", *command], cwd=data.root, capture_output=True, check=True)
            agents.write(data)
            self.assertNotIn("restore", (data.root / "AGENTS.md").read_text(encoding="utf-8"))

    def test_extensions_contract_always_and_note_when_present(self):
        text = render(MINIMAL)
        self.assertIn("register(api)", text)
        self.assertIn("test_", text)
        self.assertNotIn("уже есть расширения", text)
        self.assertIn("уже есть расширения", render(MINIMAL, has_extensions=True))

    def test_difficulty_gets_the_best_path_even_outside(self):
        text = render(MINIMAL)
        self.assertIn("даже вне Бюджеведа", text)
        self.assertIn("разбор писем или файлов", " ".join(text.split()))
        self.assertIn("чего это стоит", text)
        self.assertIn("к чему вернуться", text)

    def test_settings_after_change_rules(self):
        text = render(MINIMAL)
        self.assertIn("agents", text)
        self.assertIn("перезапусти", text)

    def test_encoding_advice_matches_readme(self):
        text = " ".join(render(MINIMAL, "win32").split())
        self.assertNotIn("PYTHONIOENCODING", text)
        self.assertIn("сам переводит свой вывод в UTF-8", text)
        self.assertIn("[Console]::OutputEncoding = [Text.Encoding]::UTF8;", text)

    def test_lending_and_borrowing_move_money_as_settlement(self):
        rows = {
            line.split("|")[1].strip(): line.split("|")[2].strip()
            for line in render(ANYA).splitlines()
            if line.startswith("| «")
        }
        self.assertEqual(rows["«одолжил <человеку> 100»"], "`--type settlement --party <ключ>`")
        self.assertEqual(rows["«занял у <человека> 100»"], "`--type settlement --paid-by <ключ>`")
        debt_rows = [value for value in rows.values() if "--type debt " in value + " "]
        self.assertEqual(len(debt_rows), 2)
        for key, value in rows.items():
            if "--type debt " in value + " ":
                self.assertIn("должен", key)
        self.assertIn("`debt` — только долг без движения денег", " ".join(render(ANYA).split()))

    def test_party_tax_and_conversion_hints_follow_settings(self):
        plain = render(MINIMAL)
        self.assertNotIn("--party", plain)
        self.assertNotIn("--tax", plain)
        customer = MINIMAL + '[people.anya]\nname = "Аня"\nrole = "покупатель"\n'
        self.assertIn("--type income --party <ключ>", render(customer))
        self.assertIn("--tax да", render(FULL))
        self.assertNotIn("--tax да", render(ANYA))
        self.assertNotIn("debt_conversion` нужен `--party", " ".join(render(ANYA).split()))
        self.assertIn("debt_conversion` нужен `--party", " ".join(render(TWO).split()))

    def test_lists_every_standard_category(self):
        text = render(MINIMAL)
        for category in (*EXPENSE_CATEGORIES, *INCOME_CATEGORIES):
            self.assertIn(category, text)

    def test_toml_examples_are_valid_settings(self):
        blocks = TOML_BLOCK.findall(render(FULL))
        self.assertTrue(blocks)
        for block in blocks:
            make_settings(block)


class WriteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = make_data(Path(tmp.name) / "Мои деньги", ANYA)
        self.path = self.data.root / "AGENTS.md"

    def test_new_file_gets_block_and_own_section(self):
        agents.write(self.data, "darwin")
        text = self.path.read_text(encoding="utf-8")
        block = agents.render_core(self.data.settings, "darwin")
        self.assertEqual(
            text, block + "\n## Моё\n\nДоговорённости, «к чему вернуться», мои расширения.\n"
        )

    def test_text_outside_block_survives_rebuilds_byte_for_byte(self):
        agents.write(self.data, "darwin")
        mine = "\r\n## Моё\r\n\r\nС Аней делим продукты пополам.  \n\nконец без перевода"
        head = "Заметка сверху\n"
        text = self.path.read_bytes().decode("utf-8")
        end = text.index(agents.END) + len(agents.END)
        self.path.write_bytes((head + text[:end] + mine).encode("utf-8"))
        agents.write(self.data, "win32")
        agents.write(self.data, "darwin")
        raw = self.path.read_bytes().decode("utf-8")
        self.assertTrue(raw.startswith(head + agents.BEGIN))
        self.assertTrue(raw.endswith(agents.END + mine))

    def test_file_without_markers_keeps_old_text_below(self):
        old = "# Мои правила\n\nстарый текст\n"
        self.path.write_text(old, encoding="utf-8")
        agents.write(self.data, "darwin")
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(agents.BEGIN))
        self.assertTrue(text.endswith(agents.END + "\n\n" + old))

    def test_damaged_markers_refuse_and_keep_the_file(self):
        damaged = "мой текст\n" + agents.END + "\n" + agents.BEGIN + "\n"
        self.path.write_text(damaged, encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "маркер.*python3 budget.py agents"):
            agents.write(self.data, "darwin")
        self.assertEqual(self.path.read_text(encoding="utf-8"), damaged)

    def test_begin_marker_alone_refuses_and_keeps_the_file(self):
        damaged = "мой текст\n" + agents.BEGIN + "\nблок без конца\n"
        self.path.write_bytes(damaged.encode("utf-8"))
        with self.assertRaisesRegex(LedgerError, "py budget.py agents"):
            agents.write(self.data, "win32")
        self.assertEqual(self.path.read_bytes(), damaged.encode("utf-8"))

    def test_crlf_file_stays_crlf(self):
        agents.write(self.data, "darwin")
        crlf = self.path.read_bytes().replace(b"\n", b"\r\n")
        self.path.write_bytes(crlf + b"\r\n## \xd0\x95\xd1\x89\xd1\x91\r\n")
        agents.write(self.data, "win32")
        raw = self.path.read_bytes()
        self.assertIn(b"py budget.py", raw)
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))

    def test_crlf_file_without_markers_stays_crlf(self):
        self.path.write_bytes("# Мои правила\r\n\r\nстарый текст\r\n".encode())
        agents.write(self.data, "darwin")
        raw = self.path.read_bytes()
        self.assertTrue(raw.endswith("\r\n\r\n# Мои правила\r\n\r\nстарый текст\r\n".encode()))
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))

    def test_agents_command_rebuilds_block_from_settings(self):
        agents.write(self.data)
        self.data.settings_file.write_text(MINIMAL, encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--data", str(self.data.root), "agents"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("AGENTS.md", out.getvalue())
        self.assertNotIn("anya", self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
