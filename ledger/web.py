"""Локальный сервер страницы-кабинета. Слушает только 127.0.0.1."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import traceback
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ledger import kaspi, ops, view
from ledger.datadir import DataDir
from ledger.journal import FIELDS, JournalError, LedgerError, format_shares, load, parse_month
from ledger.reports import format_money
from ledger.settings import Settings

DEFAULT_PORT = 8377
HOST = "127.0.0.1"
MAX_BODY = 20 * 1024 * 1024
REQUEST_TIMEOUT = 30  # секунд на чтение запроса: зависшее соединение не держит поток
CONTENT_POLICY = (
    "default-src 'self'; img-src 'self' data:; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
# Поля, которые правит страница. Книги, счета, люди, налог и чек на ней пока не размечаются,
# а долю она ставит одним числом единственному человеку (поле share).
PAGE_FIELDS = (
    "date",
    "amount",
    "currency",
    "amount2",
    "currency2",
    "category",
    "description",
    "paid_by",
    "note",
    "share",
)
DRY_RUN = {"": True, "1": True, "true": True, "0": False, "false": False}
STATIC_DIR = Path(__file__).parent / "static"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
    "/static/budzheved-wordmark.svg": ("budzheved-wordmark.svg", "image/svg+xml"),
    "/static/budzheved-dark.svg": ("budzheved-dark.svg", "image/svg+xml"),
    "/static/budzheved-icon.svg": ("budzheved-icon.svg", "image/svg+xml"),
    "/static/vendor/bulma.min.css": ("vendor/bulma.min.css", "text/css; charset=utf-8"),
}


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message, **extra}


class LedgerServer(ThreadingHTTPServer):
    daemon_threads = True
    # На Windows SO_REUSEADDR даёт занять уже занятый порт; там он не нужен.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, data: DataDir, port: int):
        super().__init__((HOST, port), Handler)
        self.data = data
        self.write_lock = threading.Lock()

    @property
    def hosts(self) -> set[str]:
        port = self.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}


def _text(data: dict, key: str, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise HttpError(400, f"поле {key} должно быть строкой")
    return value


def _field(key: str, value) -> str:
    """Значение поля из JSON: строка или целое число (доля)."""
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    raise HttpError(400, f"поле {key} должно быть строкой")


def _dry_run(query: dict) -> bool:
    if "dry_run" not in query:
        return False
    value = query["dry_run"][-1].lower()
    if value not in DRY_RUN:
        raise HttpError(400, f"dry_run должен быть 1 или 0, а не {value!r}")
    return DRY_RUN[value]


def _share(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise HttpError(400, "доля должна быть целым числом процентов") from None


def _import_payload(result: ops.ImportResult, s: Settings) -> dict:
    statement = result.statement
    assert statement is not None  # у выписки Kaspi она всегда есть
    return {
        "period_from": statement.period_from.isoformat(),
        "period_to": statement.period_to.isoformat(),
        "rows": len(statement.rows),
        "closing_text": format_money(statement.closing),
        "added": [view.entry_dict(e, s) for e in result.added],
        "missing": [view.entry_dict(e, s) for e in result.missing],
        "typed": [
            {"new": view.entry_dict(new, s), "existing": view.entry_dict(twin, s)}
            for new, twin in result.typed
        ],
        "saved": result.saved,
    }


class Handler(BaseHTTPRequestHandler):
    server: LedgerServer
    _raw: bytes | None = None
    server_version = "Budgeved"
    timeout = REQUEST_TIMEOUT

    def log_message(self, format, *args):  # noqa: A002 - сигнатура базового класса
        pass

    # --- разбор запроса -------------------------------------------------

    def _check_origin(self, writing: bool) -> None:
        hosts = self.server.hosts
        if self.headers.get("Host") not in hosts:
            raise HttpError(403, "запрос с чужого адреса")
        if writing and self.headers.get("Origin") not in {f"http://{h}" for h in hosts}:
            raise HttpError(403, "изменять журнал может только сама страница")

    def _read_body(self) -> bytes | None:
        """Тело запроса. Читается до общей блокировки: медленная отправка никого не держит."""
        header = self.headers.get("Content-Length")
        if header is None:
            return None
        keep_open = not self.close_connection
        self.close_connection = True  # при отказе тело остаётся непрочитанным
        if not header.isascii() or not header.isdecimal():
            raise HttpError(400, "неверная длина тела запроса")
        length = int(header)
        if length > MAX_BODY:
            raise HttpError(413, "слишком большой файл")
        body = self.rfile.read(length)
        if len(body) != length:
            raise HttpError(400, "тело запроса оборвалось")
        self.close_connection = not keep_open
        return body

    def _body(self) -> bytes:
        if self._raw is None:
            raise HttpError(400, "нет длины тела запроса")
        return self._raw

    def _json_body(self) -> dict:
        try:
            data = json.loads(self._body().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HttpError(400, "тело запроса не JSON") from None
        if not isinstance(data, dict):
            raise HttpError(400, "тело запроса должно быть объектом")
        return data

    def _entry_id(self, path: str) -> int:
        prefix = "/api/entries/"
        if not path.startswith(prefix) or not path[len(prefix) :].isdecimal():
            raise HttpError(404, "нет такого адреса")
        return int(path[len(prefix) :])

    # --- ответы -----------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CONTENT_POLICY)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _handle(self, writing: bool, route) -> None:
        try:
            self._check_origin(writing)
            url = urlsplit(self.path)
            query = parse_qs(url.query, keep_blank_values=True)
            if writing:
                self._raw = self._read_body()
                with self.server.write_lock:
                    status, payload = route(url.path, query)
            else:
                status, payload = route(url.path, query)
        except HttpError as exc:
            status, payload = exc.status, exc.payload
        except TimeoutError:
            self.close_connection = True
            status, payload = 408, {"error": "запрос не дошёл целиком"}
        except ops.NotFound as exc:
            status, payload = 404, {"error": str(exc)}
        except LedgerError as exc:
            status, payload = 400, {"error": str(exc)}
        except Exception:  # noqa: BLE001 - страница должна получить ответ
            traceback.print_exc(file=sys.stderr)
            status, payload = 500, {"error": "внутренняя ошибка сервера, подробности в терминале"}
        if isinstance(payload, tuple):
            self._send(status, *payload)
        else:
            self._send_json(status, payload)

    # --- маршруты ---------------------------------------------------------

    def do_GET(self):  # noqa: N802
        self._handle(False, self._get)

    def do_POST(self):  # noqa: N802
        self._handle(True, self._post)

    def do_PATCH(self):  # noqa: N802
        self._handle(True, self._patch)

    def do_DELETE(self):  # noqa: N802
        self._handle(True, self._delete)

    def _get(self, path: str, query: dict):
        if path in STATIC:
            name, content_type = STATIC[path]
            return 200, ((STATIC_DIR / name).read_bytes(), content_type)
        if path == "/api/state":
            month = parse_month(query["month"][-1]) if "month" in query else None
            settings = self.server.data.settings
            return 200, view.state(load(self.server.data.journal, settings), settings, month)
        raise HttpError(404, "нет такого адреса")

    def _post(self, path: str, query: dict):
        if path == "/api/entries":
            return self._add(self._json_body())
        if path == "/api/merchant-category":
            data = self._json_body()
            changed = ops.apply_merchant_category(
                self.server.data, _text(data, "description"), _text(data, "category")
            )
            return 200, {"updated": len(changed)}
        if path == "/api/import":
            return self._import(self._body(), _dry_run(query))
        raise HttpError(404, "нет такого адреса")

    def _add(self, data: dict):
        try:
            when = date.fromisoformat(_text(data, "date"))
            share = int(_field("share", data.get("share", 0)))
        except ValueError:
            raise HttpError(400, "неверная дата или доля") from None
        text = {
            key: _text(data, key, default)
            for key, default in (
                ("type", "expense"),
                ("amount", ""),
                ("currency", self.server.data.settings.currency),
                ("amount2", ""),
                ("currency2", ""),
                ("category", ""),
                ("description", ""),
                ("paid_by", "me"),
                ("note", ""),
            )
        }
        settings = self.server.data.settings
        result = ops.add_entry(
            self.server.data,
            date=when,
            party=ops.default_party(settings, text["type"]),
            shares=ops.single_shares(settings, share),
            force=data.get("force") is True,
            **text,
        )
        duplicates = [view.entry_dict(e, settings) for e in result.duplicates]
        if result.entry is None:
            raise HttpError(409, "похоже на дубль", duplicates=duplicates)
        return 201, {"entry": view.entry_dict(result.entry, settings)}

    def _import(self, pdf: bytes, dry_run: bool):
        # Не NamedTemporaryFile: на Windows открытый файл нельзя открыть второй раз.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "statement.pdf"
            path.write_bytes(pdf)
            text = kaspi.extract_text(path)
        settings = self.server.data.settings
        try:
            account = ops.import_account(settings, "kaspi")
        except JournalError:
            raise HttpError(
                400,
                'у выписки нет счёта: поставьте import = "kaspi" одному счёту в settings.toml '
                "или импортируйте командой import-kaspi --account",
            ) from None
        result = ops.import_statement(self.server.data, text, dry_run, account)
        if result.problems:
            raise HttpError(400, "выписка не сошлась, ничего не записано", problems=result.problems)
        return 200, _import_payload(result, settings)

    def _patch(self, path: str, query: dict):
        entry_id = self._entry_id(path)
        changes = self._json_body().get("changes")
        if not isinstance(changes, dict) or not changes:
            raise HttpError(400, "нет изменений")
        changes = {key: _field(key, value) for key, value in changes.items()}
        for key in changes:
            if key not in PAGE_FIELDS:
                if key not in FIELDS:
                    raise HttpError(400, f"нет такого поля: {key}")
                raise HttpError(400, f"поле {key} на странице не меняется")
        settings = self.server.data.settings
        if "share" in changes:  # переключатель доли: одно число для единственного человека
            shares = ops.single_shares(settings, _share(changes.pop("share")))
            changes["shares"] = format_shares(shares)
        entry = ops.edit_entry(self.server.data, entry_id, changes, view.PAGE_TYPES)
        return 200, {"entry": view.entry_dict(entry, settings)}

    def _delete(self, path: str, query: dict):
        (entry,) = ops.delete_entries(self.server.data, [self._entry_id(path)])
        return 200, {"deleted": view.entry_dict(entry, self.server.data.settings)}


def make_server(data: DataDir, port: int = 0) -> LedgerServer:
    return LedgerServer(data, port)


def serve(data: DataDir, port: int, open_browser: bool) -> int:
    try:
        server = make_server(data, port)
    except OSError as exc:
        raise JournalError(f"не удалось занять порт {port}: {exc.strerror}") from None
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"Бюджевед открыт на {url} (остановить: Ctrl+C)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
