"""Бюджевед: учёт личных финансов и взаиморасчётов. Запуск: python3 budget.py -h"""

from __future__ import annotations

import argparse
import copy
import itertools
import sys
from datetime import date
from pathlib import Path

from ledger import (
    agents,
    backups,
    export,
    extensions,
    kaspi,
    migrate,
    ops,
    reports,
    tax,
    update,
    web,
)
from ledger.calc import book_of
from ledger.datadir import DataDir
from ledger.datafolder import init_folder
from ledger.journal import (
    BUILTIN_SOURCES,
    PARTY_TYPES,
    TYPES,
    JournalError,
    LedgerError,
    load,
)
from ledger.journal import parse_month as journal_parse_month
from ledger.merchants import load_rules
from ledger.settings import ACCOUNTS_OFF, BOOKS_OFF, SETTLEMENTS_OFF, Settings, load_settings

SHARES_HELP = (
    'доли людей: ключ:процент через ;, в кавычках, например "anya:25;boris:25"; '
    "одним числом, если человек с расчётами один"
)


def parse_ids(text: str) -> list[int]:
    """«3, 7, 12–15» → [3, 7, 12, 13, 14, 15]. Принимает дефис и тире."""
    ids: list[int] = []
    for part in text.replace("–", "-").replace("—", "-").split(","):
        part = part.strip()
        if not part:
            continue
        first, dash, last = part.partition("-")
        try:
            start = int(first)
            end = int(last) if dash else start
        except ValueError:
            raise JournalError(f"не номер записи: {part!r}") from None
        if end < start:
            raise JournalError(f"диапазон задом наперёд: {part!r}")
        ids.extend(range(start, end + 1))
    if not ids:
        raise JournalError("не указан ни один номер записи")
    return ids


def data_dir(text: str) -> DataDir:
    return DataDir(Path(text).expanduser().resolve())


def parse_month(text: str) -> tuple[int, int]:
    try:
        return journal_parse_month(text)
    except JournalError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _party(args: argparse.Namespace, s: Settings) -> str:
    """Человек расчёта или долга: указанный, иначе тот, кто платил, иначе единственный."""
    if args.party:
        return args.party
    if args.type in PARTY_TYPES and args.paid_by != "me":
        return args.paid_by
    return ops.default_party(s, args.type)


def cmd_add(args: argparse.Namespace) -> int:
    settings = args.data.settings
    if args.type == "transfer" and not settings.has_accounts:
        raise LedgerError(ACCOUNTS_OFF)
    result = ops.add_entry(
        args.data,
        date=args.date,
        type=args.type,
        book=args.book,
        account=args.account,
        amount=args.amount,
        currency=args.currency or settings.currency,
        account2=args.account2,
        amount2=args.amount2,
        currency2=args.currency2,
        category=args.category,
        description=args.desc,
        paid_by=args.paid_by,
        party=_party(args, settings),
        shares=ops.shares_from_spec(settings, args.shares) if args.shares else {},
        tax=args.tax,
        source=args.source,
        receipt=args.receipt,
        note=args.note,
        force=args.force,
    )
    if result.entry is None:
        print("Похоже на дубль, запись не добавлена. Уже есть:")
        for existing in result.duplicates:
            print("  " + reports.entry_line(existing, settings))
        print("Если это другая операция, повторите команду с --force.")
        return 1
    print("Записано: " + reports.entry_line(result.entry, settings))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    settings = args.data.settings
    for e in load(args.data.journal, settings):
        if args.date_from and e.date < args.date_from:
            continue
        if args.date_to and e.date > args.date_to:
            continue
        if args.source and e.source != args.source:
            continue
        if args.book and book_of(e, settings) != args.book:
            continue
        if args.account and args.account not in (e.account, e.account2):
            continue
        print(reports.entry_line(e, settings))
    return 0


def cmd_share(args: argparse.Namespace) -> int:
    ids = parse_ids(",".join(args.ids))
    settings = args.data.settings
    if args.shares is None:
        shares = ops.default_shares(settings)
    else:
        shares = ops.shares_from_spec(settings, args.shares)
    for e in ops.set_shares(args.data, ids, shares):
        print(reports.entry_line(e, settings))
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    ids = parse_ids(",".join(args.ids))
    for e in ops.delete_entries(args.data, ids):
        print("Удалено: " + reports.entry_line(e, args.data.settings))
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    changes = {}
    for change in args.changes:
        field, equals, value = change.partition("=")
        if not equals:
            raise JournalError(f"нельзя применить изменение {change!r}")
        changes[field] = value
    updated = ops.edit_entry(args.data, args.id, changes)
    print("Изменено: " + reports.entry_line(updated, args.data.settings))
    return 0


def _extensions(data: DataDir) -> extensions.Loaded:
    """Загрузить расширения папки данных; сломанные — строкой «Внимание» и дальше без них."""
    loaded = extensions.load_extensions(data)
    for error in loaded.errors:
        print(f"Внимание: {error}", file=sys.stderr)
    return loaded


def _tax_rule_fn(data: DataDir, loaded: extensions.Loaded, book: str) -> tax.RuleFn | None:
    """Правило налога книги от расширения, под охраной и с проверкой результата."""
    rule = loaded.tax_rules.get(book)
    if rule is None:
        return None
    owner = loaded.owner("tax_rule", book)

    def run(entries, settings):
        result = extensions.guarded(
            data, owner, rule, copy.deepcopy(entries), copy.deepcopy(settings)
        )
        if not isinstance(result, tax.TaxResult) or set(result.base) != set(result.estimate):
            raise extensions.ExtensionError(
                f"расширение {owner}: правило налога должно вернуть TaxResult "
                "с одинаковыми валютами в base и estimate"
            )
        return result

    return run


def cmd_report_tax(args: argparse.Namespace, loaded: extensions.Loaded) -> int:
    settings = args.data.settings
    if args.month:
        raise JournalError("--month не подходит к report tax, период задаёт --period")
    rule = settings.tax.get(args.book)
    rule_fn = _tax_rule_fn(args.data, loaded, args.book)
    if rule is None and rule_fn is None:
        if args.book:
            raise LedgerError(f"налог для книги «{args.book}» не настроен")
        if not settings.tax and not loaded.tax_rules:
            raise LedgerError("налог не настроен: в settings.toml нет [tax.<книга>]")
    if not args.book or not args.period:
        raise JournalError("report tax требует --book и --period, например --period 2026-H2")
    # Без [tax.<книга>] налог считает расширение, и вид периода не задан: подходит любой.
    start, end = tax.parse_period(args.period, rule.period if rule else None)
    entries = load(args.data.journal, settings)
    result = tax.compute(entries, settings, args.book, start, end, rule_fn)
    print(reports.tax_report(result, args.book, args.period.strip().upper()))
    return 0


def _extension_report(args: argparse.Namespace, loaded: extensions.Loaded) -> int:
    fn = loaded.reports.get(args.kind)
    if fn is None:
        known = [*extensions.BUILTIN_REPORTS, *loaded.reports]
        raise LedgerError(f"неизвестный отчёт «{args.kind}»; есть: {', '.join(known)}")
    if args.month or args.book or args.period:
        raise JournalError(
            "--month, --book и --period подходят только встроенным отчётам; "
            "отчёту расширения аргументы передаются словами после имени"
        )
    settings = args.data.settings
    entries = tuple(copy.deepcopy(load(args.data.journal, settings)))
    owner = loaded.owner("report", args.kind)
    text = extensions.guarded(
        args.data, owner, fn, entries, copy.deepcopy(settings), list(args.args)
    )
    if not isinstance(text, str):
        raise extensions.ExtensionError(f"расширение {owner}: отчёт должен вернуть текст")
    print(text)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    settings = args.data.settings
    loaded = _extensions(args.data)
    if args.kind not in extensions.BUILTIN_REPORTS:
        return _extension_report(args, loaded)
    if args.args:
        raise JournalError(f"у отчёта {args.kind} нет аргументов: {' '.join(args.args)}")
    if args.kind == "tax":
        return cmd_report_tax(args, loaded)
    if args.kind == "debt" and not settings.has_settlements:
        raise LedgerError(SETTLEMENTS_OFF)
    if args.period:
        raise JournalError("--period подходит только к report tax")
    if args.book and args.kind != "categories":
        raise JournalError("--book подходит только к report categories и report tax")
    entries = load(args.data.journal, settings)
    if args.kind == "balance":
        print(reports.balance_report(entries, settings))
    elif args.kind == "debt":
        print(reports.debt_report(entries, settings))
    else:
        year, month = args.month or (date.today().year, date.today().month)
        print(reports.categories_report(entries, settings, year, month, args.book or None))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    entries = load(args.data.journal, args.data.settings)
    load_rules(args.data.merchants, args.data.settings.expense_categories)
    print(f"Журнал в порядке: записей {len(entries)}")
    code = 1 if _extensions(args.data).errors else 0
    tests = extensions.run_tests(args.data)
    if tests is None:
        return code
    if not tests.ok:
        print(tests.output)
        for line in tests.tampered:
            print(line)
        print("Тесты расширений не прошли")
        return 1
    print("Тесты расширений прошли")
    return code


def _rows_from(result: object, name: str) -> tuple[list, dict | None]:
    """Ответ parse: список строк или {"entries": [...], "totals": {...}}."""
    if isinstance(result, list):
        return result, None
    if isinstance(result, dict) and isinstance(result.get("entries"), list):
        totals = result.get("totals")
        if totals is None or isinstance(totals, dict):
            return result["entries"], totals
    raise extensions.ExtensionError(
        f"расширение {name}: импортёр должен вернуть список записей или словарь с entries и totals"
    )


def cmd_import(args: argparse.Namespace) -> int:
    data = args.data
    settings = data.settings
    loaded = _extensions(data)
    parse = loaded.importers.get(args.name)
    if parse is None:
        known = ", ".join(loaded.importers) or "нет ни одного"
        raise LedgerError(f"импортёра «{args.name}» нет в extensions/; есть: {known}")
    account = ops.import_account(settings, args.name, args.account)
    if not args.file.is_file():
        raise LedgerError(f"нет файла {args.file}")
    owner = loaded.owner("importer", args.name)
    rows, totals = _rows_from(extensions.guarded(data, owner, parse, args.file), owner)
    result = ops.import_rows(data, args.name, rows, totals, account, args.dry_run)
    if result.problems:
        return _refused("Итоги импорта не сошлись, ничего не записано:", result.problems)
    print(
        f"Импорт {args.name}: строк {len(rows)}, новых {len(result.added)}, "
        f"пропущено {len(result.skipped)}"
    )
    _print_entries(result.added, settings)
    _print_block("Пропущено как уже записанное:", result.skipped, settings)
    _print_tail(result, settings, args.dry_run)
    return 0


def _refused(title: str, problems: list[str]) -> int:
    """Импорт не сошёлся: заголовок и причины, код выхода 1."""
    print(title)
    for problem in problems:
        print("  " + problem)
    return 1


def _print_entries(entries, settings: Settings, indent: str = "") -> None:
    for entry in entries:
        print(indent + reports.entry_line(entry, settings))


def _print_block(title: str, entries, settings: Settings) -> None:
    if entries:
        print(title)
        _print_entries(entries, settings, "  ")


def _print_tail(result: ops.ImportResult, settings: Settings, dry_run: bool) -> None:
    """Общий конец вывода импорта: похожие ручные записи и пометка пробного запуска."""
    if result.typed:
        print(
            "Возможно, уже записано вручную (если это одна операция, лишнюю запись убирает delete):"
        )
        for entry, twin in result.typed:
            print(f"  новая #{entry.id} ↔ {reports.entry_line(twin, settings)}")
    if dry_run:
        print("Пробный запуск: журнал не изменён.")


def cmd_import_kaspi(args: argparse.Namespace) -> int:
    settings = args.data.settings
    account = ops.import_account(settings, "kaspi", args.account)
    text = kaspi.extract_text(args.pdf)
    result = ops.import_statement(args.data, text, args.dry_run, account)
    if result.problems:
        return _refused("Выписка не сошлась, ничего не записано:", result.problems)
    statement = result.statement
    assert statement is not None  # у выписки Kaspi она всегда есть
    print(
        f"Выписка {statement.period_from} — {statement.period_to}: "
        f"строк {len(statement.rows)}, новых {len(result.added)}, "
        f"сверка сошлась на {reports.format_money(statement.closing)} ₸"
    )
    _print_entries(result.added, settings)
    _print_block(
        "Есть в журнале, но нет в этой выписке (возможно, изменилась заблокированная сумма):",
        result.missing,
        settings,
    )
    _print_tail(result, settings, args.dry_run)
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    report = migrate.migrate(args.data, args.person)
    months = f"{report.months[0]} — {report.months[-1]}" if report.months else "нет"
    print(
        f"Журнал переведён на схему 2: записей {report.rows}, месяцы {months}. "
        "Отчёты balance, debt и categories за каждый месяц сошлись."
    )
    if report.payer_reset:
        print(
            "У обмена, снятия и стартового остатка плательщик теперь me (на цифры он не влиял): "
            + ", ".join(f"#{i}" for i in report.payer_reset)
        )
    print(f"Копия журнала схемы 1: {report.backup}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    data = init_folder(args.data.root, extensions.CODE_ROOT, args.currency, args.country)
    print(f"Папка данных готова: {data.root}")
    print(f"Дальше в этой папке: {agents.run_command(sys.platform)} check")
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    agents.write(args.data)
    print("AGENTS.md: блок основы перестроен по settings.toml")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    if args.name is None:
        copies = backups.listing(args.data)
        if not copies:
            print(
                "Копий журнала нет: они появляются в backups/ перед каждой записью в папке без git"
            )
            return 0
        print(f"Копии журнала в backups/, новые сверху ({len(copies)}):")
        for path in copies:
            print("  " + path.name)
        print(f"Вернуть: {agents.run_command(sys.platform)} restore ИМЯ")
        return 0
    current = backups.restore(args.data, args.name)
    print(f"Журнал возвращён из копии {args.name}")
    if current is not None:
        print(f"Журнал до возврата сохранён как {current.name}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    settings = args.data.settings
    export.require_free_target(args.data, args.excel)
    count = export.excel(load(args.data.journal, settings), settings, args.excel)
    print(f"Выгружено записей {count}: {args.excel}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    print(update.run(extensions.CODE_ROOT, args.data))
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    return web.serve(args.data, args.port, not args.no_open)


class _Disabled(argparse.Action):
    """Флаг выключенной настройками возможности: в справке его нет, команда откажет."""

    def __call__(self, parser, namespace, values, option_string=None):
        namespace.disabled = self.const


def _feature(parser, flag: str, enabled: bool, off: str, **kwargs) -> None:
    """Флаг, который есть, только когда возможность включена в settings.toml."""
    if enabled:
        parser.add_argument(flag, **kwargs)
    else:
        default = kwargs.get("default")
        parser.add_argument(
            flag, action=_Disabled, const=off, default=default, help=argparse.SUPPRESS
        )


def build_parser(s: Settings | None) -> argparse.ArgumentParser:
    """Разбор команд по настройкам папки данных.

    Без людей с расчётами, без [accounts] или без [books] их флаги и команды скрыты из справки;
    без настроек (s is None) скрыто всё необязательное.
    """
    settles = bool(s and s.has_settlements)
    accounts = list(s.accounts) if s else []
    books = list(s.books) if s and s.has_books else []
    parser = argparse.ArgumentParser(prog="budget.py", description=__doc__)
    parser.add_argument(
        "--data",
        type=data_dir,
        required=True,
        metavar="PATH",
        help="папка данных: журнал, правила категорий и настройки",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    visible: list[str] = []

    def command(name: str, help_text: str, shown: bool = True) -> argparse.ArgumentParser:
        if not shown:  # add_parser(help=SUPPRESS) печатает «==SUPPRESS==» в списке команд
            return commands.add_parser(name)
        visible.append(name)
        return commands.add_parser(name, help=help_text)

    add = command("add", "добавить запись")
    add.add_argument("--date", type=date.fromisoformat, default=date.today())
    shown_types = TYPES if accounts else tuple(t for t in TYPES if t != "transfer")
    add.add_argument(
        "--type", choices=TYPES, default="expense", metavar="{" + ",".join(shown_types) + "}"
    )
    _feature(
        add,
        "--book",
        bool(books),
        BOOKS_OFF,
        choices=books,
        default="",
        help="книга; по умолчанию книга счёта или книга по умолчанию",
    )
    _feature(
        add,
        "--account",
        bool(accounts),
        ACCOUNTS_OFF,
        choices=accounts,
        default="",
        help="счёт из settings.toml",
    )
    add.add_argument("--amount", required=True)
    owner = s.currency if s else None
    add.add_argument(
        "--currency", default=owner, help=f"по умолчанию {owner or 'валюта владельца'}"
    )
    _feature(
        add,
        "--account2",
        bool(accounts),
        ACCOUNTS_OFF,
        choices=accounts,
        default="",
        help="второй счёт: куда перевод или обмен",
    )
    add.add_argument("--amount2", default="")
    add.add_argument("--currency2", default="")
    add.add_argument("--category", default="")
    add.add_argument("--desc", default="")
    add.add_argument(
        "--party",
        default="",
        help="ключ человека из settings.toml" if s and s.people else argparse.SUPPRESS,
    )
    _feature(
        add,
        "--paid-by",
        settles,
        SETTLEMENTS_OFF,
        default="me",
        help="me или ключ человека с расчётами",
    )
    _feature(add, "--shares", settles, SETTLEMENTS_OFF, metavar="SPEC", help=SHARES_HELP)
    add.add_argument(
        "--tax", choices=("да", "нет"), default="", help="облагается ли налогом вопреки правилу"
    )
    add.add_argument("--receipt", default="", help="путь к чеку внутри receipts/")
    add.add_argument("--source", choices=BUILTIN_SOURCES, default="text")
    add.add_argument("--note", default="")
    add.add_argument("--force", action="store_true", help="записать, даже если похоже на дубль")
    add.set_defaults(handler=cmd_add)

    listing = command("list", "показать записи")
    listing.add_argument("--from", dest="date_from", type=date.fromisoformat)
    listing.add_argument("--to", dest="date_to", type=date.fromisoformat)
    listing.add_argument("--source", help="text, screenshot, receipt, kaspi-pdf или import:<имя>")
    _feature(listing, "--book", bool(books), BOOKS_OFF, choices=books, help="только эта книга")
    _feature(
        listing,
        "--account",
        bool(accounts),
        ACCOUNTS_OFF,
        choices=accounts,
        help="только этот счёт",
    )
    listing.set_defaults(handler=cmd_list)

    share = command("share", "проставить расходам доли людей с расчётами", settles)
    share.add_argument("ids", nargs="+", help="номера и диапазоны: 3,7,12-15")
    share.add_argument(
        "--shares", metavar="SPEC", help=SHARES_HELP + "; без него default_share из settings.toml"
    )
    share.set_defaults(handler=cmd_share)
    if not settles:
        share.set_defaults(disabled=SETTLEMENTS_OFF)

    delete = command("delete", "удалить записи")
    delete.add_argument("ids", nargs="+", help="номера и диапазоны: 3,7,12-15")
    delete.set_defaults(handler=cmd_delete)

    edit = command("edit", "изменить поля записи")
    edit.add_argument("id", type=int)
    edit.add_argument("changes", nargs="+", metavar="поле=значение")
    edit.set_defaults(handler=cmd_edit)

    report = command("report", "отчёты")
    kinds = extensions.BUILTIN_REPORTS
    shown_kinds = kinds if settles else tuple(k for k in kinds if k != "debt")
    report.add_argument(
        "kind",
        metavar="{" + ",".join(shown_kinds) + "}",
        help="встроенный отчёт или имя отчёта из extensions/",
    )
    report.add_argument("args", nargs="*", metavar="ARG", help="аргументы отчёта расширения")
    report.add_argument("--month", type=parse_month, help="ГГГГ-ММ, по умолчанию текущий")
    _feature(
        report,
        "--book",
        bool(books),
        BOOKS_OFF,
        choices=books,
        default="",
        help="для categories: только эта книга; для tax: книга налога",
    )
    report.add_argument(
        "--period",
        default="",
        help="для tax: период по настройке книги: 2026-10, 2026-Q3, 2026-H2 или 2026",
    )
    report.set_defaults(handler=cmd_report)

    check = command("check", "проверить журнал, правила категорий и тесты расширений")
    check.set_defaults(handler=cmd_check)

    account_help = "счёт строк; по умолчанию единственный счёт с import = {}"
    importing = command("import", "импортировать файл импортёром из extensions/")
    importing.add_argument("name", metavar="NAME", help="имя импортёра")
    importing.add_argument("file", type=Path, metavar="FILE")
    _feature(
        importing,
        "--account",
        bool(accounts),
        ACCOUNTS_OFF,
        choices=accounts,
        default="",
        help=account_help.format("NAME"),
    )
    importing.add_argument("--dry-run", action="store_true")
    importing.set_defaults(handler=cmd_import)

    import_kaspi = command("import-kaspi", "импортировать выписку Kaspi Gold")
    import_kaspi.add_argument("pdf", type=Path)
    _feature(
        import_kaspi,
        "--account",
        bool(accounts),
        ACCOUNTS_OFF,
        choices=accounts,
        default="",
        help=account_help.format('"kaspi"'),
    )
    import_kaspi.add_argument("--dry-run", action="store_true")
    import_kaspi.set_defaults(handler=cmd_import_kaspi)

    migration = command("migrate", "перевести журнал схемы 1 на схему 2 со сверкой отчётов")
    migration.add_argument(
        "--person",
        required=True,
        metavar="KEY",
        help="ключ человека из settings.toml, с которым велись расчёты в схеме 1",
    )
    migration.set_defaults(handler=cmd_migrate)

    init = command("init", "создать папку данных: настройки, журнал, budget.py, AGENTS.md")
    init.add_argument("--currency", default="USDT", help="валюта владельца, по умолчанию USDT")
    init.add_argument(
        "--country", default="XX", metavar="XX", help="код страны, например KZ; можно позже"
    )
    init.set_defaults(handler=cmd_init)

    agents_command = command("agents", "перестроить блок основы в AGENTS.md по settings.toml")
    agents_command.set_defaults(handler=cmd_agents)

    restore = command("restore", "показать копии журнала из backups/ или вернуть журнал из копии")
    restore.add_argument("name", nargs="?", metavar="ИМЯ", help="имя копии из списка")
    restore.set_defaults(handler=cmd_restore)

    exporting = command("export", "выгрузить журнал в таблицу")
    exporting.add_argument(
        "--excel",
        type=Path,
        required=True,
        metavar="FILE",
        help="CSV для Excel: разделитель ;, десятичная запятая, UTF-8 с BOM",
    )
    exporting.set_defaults(handler=cmd_export)

    updating = command("update", "обновить код Бюджеведа и перестроить AGENTS.md")
    updating.set_defaults(handler=cmd_update)

    ui = command("ui", "открыть страницу-кабинет в браузере")
    ui.add_argument("--port", type=int, default=web.DEFAULT_PORT)
    ui.add_argument("--no-open", action="store_true", help="не открывать браузер")
    ui.set_defaults(handler=cmd_ui)
    commands.metavar = "{" + ",".join(visible) + "}"
    return parser


def _before_command(argv: list[str]) -> list[str]:
    """Аргументы до подкоманды. --data стоит только там, а сокращённый флаг подкоманды
    (--da вместо --date) иначе приняли бы за --data."""
    head: list[str] = []
    tokens = iter(argv)
    for token in tokens:
        if not token.startswith("-"):
            break  # первое слово без дефиса, не значение флага: подкоманда
        head.append(token)
        if "=" not in token and len(token) > 2 and "--data".startswith(token):
            head.extend(itertools.islice(tokens, 1))  # значение --data
    return head


def _configured(argv: list[str] | None) -> DataDir | None:
    """Первый проход: папка из --data, если в ней есть settings.toml."""
    pre = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    pre.add_argument("--data")
    head = _before_command(sys.argv[1:] if argv is None else argv)
    try:
        known, _ = pre.parse_known_args(head)
    except argparse.ArgumentError:
        return None  # второй проход скажет, что не так
    if not known.data:
        return None
    data = data_dir(known.data)
    return data if data.settings_file.exists() else None


def _settings(configured: DataDir | None, argv: list[str] | None) -> Settings | None:
    """Настройки папки для разбора команд. update работает и с настройками, которые этот код
    не читает (например, схема новее кода): как раз он ставит код, который их прочтёт."""
    if configured is None:
        return None
    try:
        return configured.settings
    except LedgerError:
        argv = sys.argv[1:] if argv is None else argv
        rest = argv[len(_before_command(argv)) :]
        if rest[:1] == ["update"]:
            return None
        raise


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Консоль Windows по умолчанию в cp1251/cp866; у подменённого потока (тесты) метода нет.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        configured = _configured(argv)
        settings = _settings(configured, argv)
        args = build_parser(settings).parse_args(argv)
        if args.data == configured:
            args.data = configured  # настройки уже прочитаны
        disabled = getattr(args, "disabled", None)
        if disabled:
            if settings is None:  # нет settings.toml: сначала сказать об этом
                load_settings(args.data.settings_file)
            raise LedgerError(disabled)
        return args.handler(args)
    except LedgerError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
