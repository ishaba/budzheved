import contextlib
import hashlib
import io
import json
import re
import socket
import tempfile
import threading
import unittest
from datetime import date
from decimal import Decimal
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

from ledger import cli, ops, web
from ledger.journal import load as load_journal
from ledger.settings import SETTLEMENTS_OFF
from tests.helpers import ANYA, MINIMAL, make_data

FIXTURE = Path(__file__).parent / "fixtures" / "kaspi_sample.txt"
TWO = ANYA + '[people.boris]\nname = "Борис"\nrole = "партнёр"\nsettlements = true\n'


class WebTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = make_data(Path(tmp.name) / "Мои деньги", ANYA)
        self.journal = self.data.journal
        self.empty = self.journal.read_bytes()
        self.merchants = self.data.merchants
        self.merchants.write_text("pattern,category\nтакси,транспорт\n", encoding="utf-8")
        self.server = web.make_server(self.data)
        self.port = self.server.server_address[1]
        thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def load(self):
        return load_journal(self.journal, self.data.settings)

    def request(self, method, path, body=None, headers=None, raw=None):
        base = {"Host": f"127.0.0.1:{self.port}"}
        if method != "GET":
            base["Origin"] = f"http://127.0.0.1:{self.port}"
        base.update(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            base["Content-Type"] = "application/json"
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        connection.putrequest(method, path, skip_host=True)
        if data is not None:
            base["Content-Length"] = str(len(data))
        for name, value in base.items():
            if value is not None:
                connection.putheader(name, value)
        connection.endheaders(data)
        response = connection.getresponse()
        payload = response.read()
        if response.getheader("Content-Type", "").startswith("application/json"):
            payload = json.loads(payload)
        return response.status, payload

    def add(self, **fields):
        fields.setdefault("date", "2026-10-01")
        status, payload = self.request("POST", "/api/entries", fields)
        self.assertEqual(status, 201, payload)
        return payload["entry"]

    # --- страница и состояние ---------------------------------------------

    def test_serves_page_files_only(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", body.lower())
        self.assertEqual(self.request("GET", "/static/app.js")[0], 200)
        self.assertEqual(self.request("GET", "/static/style.css")[0], 200)
        for name in ("budzheved-wordmark.svg", "budzheved-dark.svg", "budzheved-icon.svg"):
            with self.subTest(name=name):
                status, body = self.request("GET", f"/static/{name}")
                self.assertEqual(status, 200)
                self.assertTrue(body.startswith(b"<svg"))
        index = self.request("GET", "/")[1].decode("utf-8")
        for reference in (
            "/static/budzheved-wordmark.svg",
            "/static/budzheved-dark.svg",
            "/static/budzheved-icon.svg",
        ):
            self.assertIn(reference, index)
        for path in (
            "/static/../journal.py",
            "/data/journal.csv",
            "/static/web.py",
            "/api/nope",
            "/static/budzheved-mark.svg",
            "/favicon.ico",
            "/static/vendor/README.md",
            "/static/vendor/../app.js",
            "/bulma",
            "/halfmoon",
            "/static/bulma.css",
            "/static/bulma-classes.js",
            "/static/variant.css",
            "/static/vendor/halfmoon.min.css",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_page_references_only_served_files(self):
        html = self.request("GET", "/")[1].decode("utf-8")
        references = set(re.findall(r'(?:href|src|srcset)="(/static/[^"]+)"', html))
        self.assertLessEqual(
            {"/static/app.js", "/static/style.css", "/static/vendor/bulma.min.css"}, references
        )
        for reference in sorted(references):
            with self.subTest(reference=reference):
                status, body = self.request("GET", reference)
                self.assertEqual(status, 200)
                self.assertTrue(body)

    def test_page_loads_nothing_from_the_network(self):
        html = self.request("GET", "/")[1].decode("utf-8")
        self.assertNotRegex(html, r'(?:href|src|srcset)="(?:https?:)?//')
        for path, (_name, content_type) in web.STATIC.items():
            if content_type.startswith("text/css"):
                with self.subTest(path=path):
                    css = self.request("GET", path)[1].decode("utf-8")
                    self.assertNotIn("@import", css)
                    external = re.search(r"url\(\s*(?![\"']?data:)[^)]{0,60}", css)
                    self.assertIsNone(external, external and external.group(0))

    def test_vendor_files_match_recorded_checksums(self):
        vendor = web.STATIC_DIR / "vendor"
        recorded = re.findall(
            r"^\| `([^`]+)` .*`([0-9a-f]{64})` \|$",
            (vendor / "README.md").read_text(encoding="utf-8"),
            re.M,
        )
        self.assertEqual({name for name, _ in recorded}, {"bulma.min.css"})
        for name, checksum in recorded:
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((vendor / name).read_bytes()).hexdigest(), checksum)

    def test_state_of_empty_and_filled_journal(self):
        status, state = self.request("GET", "/api/state")
        self.assertEqual((status, state["entries"]), (200, []))
        self.add(amount="1500", currency="KZT", description="такси")
        status, state = self.request("GET", "/api/state?month=2026-10")
        self.assertEqual(state["entries"][0]["category"], "транспорт")
        self.assertEqual(
            state["balances"], [{"currency": "KZT", "has_opening": False, "text": "-1 500,00"}]
        )
        self.assertEqual(self.request("GET", "/api/state?month=october")[0], 400)

    # --- записи -------------------------------------------------------------

    def test_add_edit_delete_entry(self):
        entry = self.add(amount="1 500,50", currency="kzt", description="кофе")
        self.assertEqual((entry["id"], entry["amount"], entry["currency"]), (1, "1500.50", "KZT"))
        status, payload = self.request(
            "PATCH", "/api/entries/1", {"changes": {"category": "кафе и доставка", "share": 50}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            (payload["entry"]["category"], payload["entry"]["share"]), ("кафе и доставка", 50)
        )
        self.assertEqual(payload["entry"]["shares"], "anya:50")
        self.assertEqual(self.load()[0].shares, {"anya": 50})
        status, payload = self.request("PATCH", "/api/entries/1", {"changes": {"share": "0"}})
        self.assertEqual((status, payload["entry"]["shares"]), (200, ""))
        status, payload = self.request("DELETE", "/api/entries/1")
        self.assertEqual((status, payload["deleted"]["id"]), (200, 1))
        self.assertEqual(self.load(), [])

    def test_settlement_and_debt_get_the_single_person(self):
        settlement = self.add(type="settlement", amount="300", paid_by="anya")
        debt = self.add(type="debt", amount="850", date="2026-10-02")
        self.assertEqual((settlement["party"], debt["party"]), ("anya", "anya"))
        self.assertIn("(с Аня)", settlement["line"])
        status, state = self.request("GET", "/api/state")
        self.assertEqual([p["value"] for p in state["options"]["payers"]], ["me", "anya"])

    def test_share_without_a_settlement_person_is_refused(self):
        # Сервер держит прочитанные настройки, поэтому ему дают заново созданную папку.
        self.data = self.server.data = make_data(self.data.root, MINIMAL)
        self.add(amount="10")
        self.assertIsNone(self.request("GET", "/api/state")[1]["share_person"])
        status, payload = self.request("PATCH", "/api/entries/1", {"changes": {"share": 50}})
        self.assertEqual((status, payload), (400, {"error": SETTLEMENTS_OFF}))
        self.assertIn("взаиморасчёты выключены", payload["error"])
        status, payload = self.request(
            "PATCH", "/api/entries/1", {"changes": {"share": "половина"}}
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.load()[0].shares, {})

    def test_one_settlement_person_is_the_share_person(self):
        status, state = self.request("GET", "/api/state")
        self.assertEqual(state["share_person"], {"key": "anya", "name": "Аня"})

    def test_two_people_keep_their_shares_when_other_fields_change(self):
        self.data = self.server.data = make_data(self.data.root, TWO)
        self.add(amount="1000", currency="KZT", description="ужин")
        ops.set_shares(self.data, [1], {"anya": 50, "boris": 25})
        self.assertIsNone(self.request("GET", "/api/state")[1]["share_person"])
        status, payload = self.request(
            "PATCH",
            "/api/entries/1",
            {"changes": {"category": "кафе и доставка", "amount": "1200", "note": "с чаем"}},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["entry"]["shares"], "anya:50;boris:25")
        before = self.journal.read_bytes()
        status, payload = self.request("PATCH", "/api/entries/1", {"changes": {"share": 50}})
        self.assertEqual(status, 400)
        self.assertIn("людей с расчётами 2", payload["error"])
        self.assertEqual(self.journal.read_bytes(), before)
        (entry,) = self.load()
        self.assertEqual(
            (entry.category, entry.amount, entry.shares),
            ("кафе и доставка", Decimal("1200"), {"anya": 50, "boris": 25}),
        )

    def test_add_writes_the_new_fields_empty(self):
        self.use(
            MINIMAL.replace('"USDT"', '"KZT"')
            + '[books]\ndefault = "личное"\nnames = ["личное", "дело"]\n'
            + '[accounts.card]\nbook = "дело"\ncurrencies = ["KZT"]\n'
            + '[people.anya]\nname = "Аня"\nrole = "партнёр"\nsettlements = true\n'
        )
        self.add(
            amount="700",
            description="такси",
            share=50,
            book="дело",
            account="card",
            party="anya",
            tax="да",
            receipt="receipts/1.jpg",
        )
        (entry,) = self.load()
        self.assertEqual(entry.currency, "KZT")  # валюта владельца, а не USDT
        self.assertEqual(entry.shares, {"anya": 50})
        self.assertEqual(
            (entry.book, entry.account, entry.account2, entry.party, entry.tax, entry.receipt),
            ("", "", "", "", "", ""),
        )
        status, state = self.request("GET", "/api/state")
        self.assertEqual(state["options"]["currencies"][0], "KZT")

    def test_patch_does_not_write_books_accounts_and_people(self):
        self.use(TWO + '[accounts.card]\ncurrencies = ["USDT"]\n')
        self.add(amount="10")
        before = self.journal.read_bytes()
        for changes in (
            {"book": "дело"},
            {"account": "card"},
            {"account2": "card"},
            {"party": "anya"},
            {"shares": "anya:50"},
            {"tax": "да"},
            {"receipt": "receipts/1.jpg"},
            {"type": "income"},
        ):
            with self.subTest(changes=changes):
                status, payload = self.request("PATCH", "/api/entries/1", {"changes": changes})
                self.assertEqual(status, 400, payload)
                self.assertEqual(
                    payload["error"], f"поле {next(iter(changes))} на странице не меняется"
                )
        status, payload = self.request("PATCH", "/api/entries/1", {"changes": {"bogus": "x"}})
        self.assertEqual((status, payload), (400, {"error": "нет такого поля: bogus"}))
        self.assertEqual(self.journal.read_bytes(), before)

    def test_entries_without_a_page_form_are_not_patched(self):
        self.use(
            ANYA
            + '[accounts.card]\ncurrencies = ["USDT"]\n[accounts.cash]\ncurrencies = ["USDT"]\n'
        )
        ops.add_entry(
            self.data,
            date=date(2026, 10, 1),
            type="transfer",
            account="card",
            account2="cash",
            amount="10",
        )
        before = self.journal.read_bytes()
        for changes in ({"paid_by": "anya"}, {"amount": "20"}):
            with self.subTest(changes=changes):
                status, payload = self.request("PATCH", "/api/entries/1", {"changes": changes})
                self.assertEqual(
                    (status, payload), (400, {"error": "запись правится через агента"})
                )
        self.assertEqual(self.journal.read_bytes(), before)

    def test_probable_duplicate_is_409_until_forced(self):
        self.add(amount="12")
        status, payload = self.request(
            "POST", "/api/entries", {"date": "2026-10-02", "amount": "12"}
        )
        self.assertEqual(status, 409)
        self.assertEqual([e["id"] for e in payload["duplicates"]], [1])
        self.assertEqual(len(self.load()), 1)
        status, _ = self.request(
            "POST", "/api/entries", {"date": "2026-10-02", "amount": "12", "force": True}
        )
        self.assertEqual(status, 201)

    def test_bad_requests_change_nothing(self):
        self.add(amount="10")
        before = self.journal.read_bytes()
        cases = [
            ("POST", "/api/entries", {"date": "2026-10-01", "amount": "abc"}, 400),
            ("POST", "/api/entries", {"date": "вчера", "amount": "5"}, 400),
            (
                "POST",
                "/api/entries",
                {"date": "2026-10-01", "amount": "5", "currency": "EURO"},
                400,
            ),
            ("PATCH", "/api/entries/1", {"changes": {"category": "метро"}}, 400),
            ("PATCH", "/api/entries/1", {"changes": {"amount": "1e30"}}, 400),
            ("POST", "/api/entries", {"date": "2026-10-01", "amount": "1e30"}, 400),
            ("PATCH", "/api/entries/1", {"changes": {}}, 400),
            ("PATCH", "/api/entries/1", {"changes": {"id": "5"}}, 400),
            ("PATCH", "/api/entries/9", {"changes": {"note": "x"}}, 404),
            ("DELETE", "/api/entries/9", None, 404),
            ("DELETE", "/api/entries/abc", None, 404),
            ("POST", "/api/unknown", {}, 404),
        ]
        for method, path, body, expected in cases:
            with self.subTest(method=method, path=path, body=body):
                status, payload = self.request(method, path, body)
                self.assertEqual(status, expected, payload)
                self.assertIn("error", payload)
        status, _ = self.request(
            "POST", "/api/entries", raw=b"{not json", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.journal.read_bytes(), before)

    def test_merchant_category(self):
        self.add(amount="500", currency="KZT", description="NOVA")
        self.add(amount="900", currency="KZT", description="NOVA", force=True)
        status, payload = self.request(
            "POST", "/api/merchant-category", {"description": "NOVA", "category": "одежда"}
        )
        self.assertEqual((status, payload), (200, {"updated": 2}))
        self.assertEqual([e.category for e in self.load()], ["одежда", "одежда"])
        self.assertIn("=nova,одежда", self.merchants.read_text(encoding="utf-8"))

    # --- защита -------------------------------------------------------------

    def test_foreign_host_is_refused(self):
        status, payload = self.request("GET", "/api/state", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.request("GET", "/", headers={"Host": f"evil.example:{self.port}"})
        self.assertEqual(status, 403)
        status, _ = self.request("GET", "/api/state", headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(status, 200)

    def test_writes_need_the_pages_own_origin(self):
        body = {"date": "2026-10-01", "amount": "5"}
        for origin in (None, "http://evil.example", "null", f"https://127.0.0.1:{self.port}"):
            with self.subTest(origin=origin):
                status, _ = self.request("POST", "/api/entries", body, headers={"Origin": origin})
                self.assertEqual(status, 403)
        self.add(amount="5")
        for method in ("PATCH", "DELETE"):
            status, _ = self.request(
                method,
                "/api/entries/1",
                {"changes": {"note": "x"}},
                headers={"Origin": "http://evil.example"},
            )
            self.assertEqual(status, 403)
        self.assertEqual(len(self.load()), 1)
        self.assertEqual(self.load()[0].note, "")

    def test_oversized_body_is_refused_before_reading(self):
        status, _ = self.request(
            "POST", "/api/import", headers={"Content-Length": str(web.MAX_BODY + 1)}
        )
        self.assertEqual(status, 413)

    # --- выписка ------------------------------------------------------------

    def upload(self, path, text=None, pdf=b"%PDF-1.4 fake"):
        text = FIXTURE.read_text(encoding="utf-8") if text is None else text
        seen = {}

        def fake_extract(pdf_path):
            seen["path"] = pdf_path
            seen["bytes"] = pdf_path.read_bytes()
            return text

        with mock.patch("ledger.kaspi.extract_text", side_effect=fake_extract):
            status, payload = self.request(
                "POST", path, raw=pdf, headers={"Content-Type": "application/pdf"}
            )
        return status, payload, seen

    def test_import_preview_then_import(self):
        self.add(amount="1000", currency="KZT", description="кофе Lumen")
        status, preview, seen = self.upload("/api/import?dry_run=1")
        self.assertEqual(status, 200, preview)
        self.assertEqual(seen["bytes"], b"%PDF-1.4 fake")
        self.assertFalse(seen["path"].exists())
        self.assertEqual(
            (preview["rows"], len(preview["added"]), preview["saved"], preview["closing_text"]),
            (9, 9, False, "31 106,39"),
        )
        self.assertEqual(
            (preview["period_from"], preview["period_to"]), ("2026-10-01", "2026-10-03")
        )
        self.assertEqual(
            [(pair["new"]["id"], pair["existing"]["id"]) for pair in preview["typed"]], [(4, 1)]
        )
        self.assertEqual(len(self.load()), 1)
        status, done, _ = self.upload("/api/import")
        self.assertEqual((status, done["saved"], len(done["added"])), (200, True, 9))
        self.assertEqual(len(self.load()), 10)
        status, again, _ = self.upload("/api/import")
        self.assertEqual((again["saved"], again["added"]), (False, []))

    def test_import_that_does_not_reconcile(self):
        text = FIXTURE.read_text(encoding="utf-8").replace(
            "- 494,00 ₸                Разное", "- 400,00 ₸                Разное"
        )
        status, payload, _ = self.upload("/api/import", text=text)
        self.assertEqual(status, 400)
        self.assertEqual(len(payload["problems"]), 2)
        self.assertEqual(self.journal.read_bytes(), self.empty)

    def use(self, toml):
        self.data.settings_file.write_text(toml, encoding="utf-8")
        self.data.__dict__.pop("settings", None)  # настройки перечитываются

    def test_import_goes_to_the_kaspi_account(self):
        two = MINIMAL.replace('currency = "USDT"', 'currency = "KZT"') + (
            '[accounts.card]\ncurrencies = ["KZT"]\nimport = "kaspi"\n'
            '[accounts.spare]\ncurrencies = ["KZT"]\nimport = "kaspi"\n'
        )
        self.use(two)
        status, payload, _ = self.upload("/api/import")
        self.assertEqual(status, 400)
        self.assertIn('import = "kaspi"', payload["error"])
        self.assertEqual(self.journal.read_bytes(), self.empty)
        self.use(two.replace('"kaspi"', '"other"', 1))
        status, payload, _ = self.upload("/api/import")
        self.assertEqual(status, 200, payload)
        self.assertEqual({e.account for e in self.load()}, {"spare"})

    def test_import_of_something_that_is_not_a_statement(self):
        status, payload, _ = self.upload("/api/import", text="Счёт на оплату\n")
        self.assertEqual(status, 400)
        self.assertIn("не похоже на выписку Kaspi", payload["error"])

    def test_dry_run_flag_is_read_strictly(self):
        for query in ("dry_run", "dry_run=", "dry_run=1", "dry_run=true"):
            status, payload, _ = self.upload("/api/import?" + query)
            self.assertEqual((status, payload.get("saved")), (200, False), query)
            self.assertEqual(self.journal.read_bytes(), self.empty, query)
        status, payload, _ = self.upload("/api/import?dry_run=maybe")
        self.assertEqual(status, 400)
        self.assertEqual(self.journal.read_bytes(), self.empty)
        status, payload, _ = self.upload("/api/import?dry_run=0")
        self.assertEqual((status, payload["saved"]), (200, True))

    # --- устойчивость сервера -----------------------------------------------

    def raw_socket(self, head):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(head.encode("ascii"))
        return sock

    def stalled_upload(self):
        return self.raw_socket(
            "POST /api/entries HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\nOrigin: http://127.0.0.1:{self.port}\r\n"
            "Content-Type: application/json\r\nContent-Length: 50\r\n\r\n"
        )

    def test_connection_closes_after_a_write(self):
        body = b'{"amount": "5", "date": "2026-10-01"}'
        sock = self.raw_socket(
            "POST /api/entries HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\nOrigin: http://127.0.0.1:{self.port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        )
        sock.sendall(body)
        sock.settimeout(2)
        answer = b""
        while chunk := sock.recv(4096):  # сервер HTTP/1.0: после ответа соединение закрывается
            answer += chunk
        self.assertIn(b" 201 ", answer.split(b"\r\n")[0])

    def test_negative_body_length_is_refused(self):
        status, payload = self.request("POST", "/api/import", headers={"Content-Length": "-1"})
        self.assertEqual(status, 400)
        self.assertIn("длин", payload["error"])

    def test_stalled_upload_does_not_block_other_writes(self):
        self.stalled_upload()
        entry = self.add(amount="5", description="пока другой запрос висит")
        self.assertEqual(entry["id"], 1)

    def test_stalled_request_times_out(self):
        with mock.patch.object(web.Handler, "timeout", 0.3):
            sock = self.stalled_upload()
            answer = sock.recv(4096)
        self.assertIn(b" 408 ", answer.split(b"\r\n")[0])
        self.assertEqual(self.journal.read_bytes(), self.empty)

    def test_pages_cannot_be_framed(self):
        for path in ("/", "/api/state", "/nope"):
            connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
            self.addCleanup(connection.close)
            connection.request("GET", path)
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.getheader("X-Frame-Options"), "DENY", path)
            self.assertIn(
                "frame-ancestors 'none'", response.getheader("Content-Security-Policy"), path
            )

    def test_values_of_the_wrong_json_type_are_refused(self):
        bad_entries = [
            {"amount": "5", "date": "2026-10-01", "description": None},
            {"amount": 5.5, "date": "2026-10-01"},
            {"amount": "5", "date": "2026-10-01", "share": None},
            {"amount": "5", "date": "2026-10-01", "share": True},
            {"amount": "5", "date": "2026-10-01", "note": ["x"]},
            {"amount": "5", "date": None},
        ]
        for body in bad_entries:
            status, payload = self.request("POST", "/api/entries", body)
            self.assertEqual(status, 400, body)
            self.assertNotIn("внутренняя", payload["error"], body)
        self.assertEqual(self.journal.read_bytes(), self.empty)
        self.add(amount="5", description="кофе", share=50)
        for changes in ({"note": None}, {"description": {"a": 1}}, {"share": False}):
            status, payload = self.request("PATCH", "/api/entries/1", {"changes": changes})
            self.assertEqual(status, 400, changes)
        status, _ = self.request(
            "POST", "/api/merchant-category", {"description": None, "category": "транспорт"}
        )
        self.assertEqual(status, 400)
        (entry,) = self.load()
        self.assertEqual((entry.description, entry.note, entry.shares), ("кофе", "", {"anya": 50}))
        status, _ = self.request("PATCH", "/api/entries/1", {"changes": {"share": 100}})
        self.assertEqual(status, 200)

    def test_internal_errors_do_not_leak_details(self):
        stderr = io.StringIO()
        with (
            mock.patch("ledger.view.state", side_effect=RuntimeError("/секретный/путь")),
            contextlib.redirect_stderr(stderr),
        ):
            status, payload = self.request("GET", "/api/state")
        self.assertEqual(status, 500)
        self.assertNotIn("секретный", payload["error"])
        self.assertIn("секретный", stderr.getvalue())


class ServeTest(unittest.TestCase):
    def test_ui_command_reports_a_busy_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = make_data(Path(tmp))
            holder = web.make_server(data)
            self.addCleanup(holder.server_close)
            port = str(holder.server_address[1])
            err = io.StringIO()
            with contextlib.redirect_stderr(err), mock.patch("webbrowser.open") as opened:
                code = cli.main(["--data", str(data.root), "ui", "--port", port])
            self.assertEqual(code, 1)
            self.assertIn(f"Ошибка: не удалось занять порт {port}", err.getvalue())
            opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
