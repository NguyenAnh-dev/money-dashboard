from __future__ import annotations

import argparse
import sys
import unicodedata

import sync
from adapters.csv_source import (
    CONNECTION_ID,
    CsvAccount,
    CsvSource,
    ParsedRow,
    read_rows,
)
from core import store, users
from core.models import scoped_connection_id
from core.money import format_money

SAMPLE_ROWS = 5
_UNSAFE = ("Cc", "Cf", "Zl", "Zp")


def safe(text: object) -> str:
    out = []
    for ch in str(text):
        if unicodedata.category(ch) in _UNSAFE:
            code = ord(ch)
            if code < 0x100:
                out.append(f"\\x{code:02x}")
            elif code < 0x10000:
                out.append(f"\\u{code:04x}")
            else:
                out.append(f"\\U{code:08x}")
        else:
            out.append(ch)
    return "".join(out)


def validate(path: str, account_id: str) -> tuple[str, str]:
    if not path or not path.strip():
        raise ValueError("file argument must not be empty")
    account_id = account_id.strip()
    if not account_id:
        raise ValueError("account identifier must not be empty")
    if any(unicodedata.category(ch) in _UNSAFE for ch in account_id):
        raise ValueError(
            "account identifier must not contain control or line-break characters"
        )
    return path, account_id


def build_source(path: str, account_id: str, user_id: str,
                 rows: list[ParsedRow] | None = None) -> CsvSource:
    path, account_id = validate(path, account_id)
    return CsvSource(
        path=path,
        account=CsvAccount(provider_id=account_id, name=account_id),
        connection_id=scoped_connection_id(f"{CONNECTION_ID}/{account_id}", user_id),
        rows=rows,
    )


def parse(path: str, account_id: str) -> list[ParsedRow]:
    path, account_id = validate(path, account_id)
    rows = read_rows(path)
    if not rows:
        raise ValueError(f"{path}: no transactions found")
    return rows


def preview(account_id: str, rows: list[ParsedRow]) -> str:
    ordered = sorted(rows, key=lambda r: r.date)
    lines = [
        f"account   {safe(account_id.strip())}",
        f"dates     {ordered[0].date.isoformat()} to {ordered[-1].date.isoformat()}",
        f"rows      {len(rows)}",
        "sample",
    ]
    for row in ordered[:SAMPLE_ROWS]:
        label = "charge" if row.amount_cents > 0 else "credit"
        lines.append(
            f"  {row.date.isoformat()}  {label:<6}  "
            f"{format_money(abs(row.amount_cents)):>10}  {safe(row.description)}"
        )
    if len(ordered) > SAMPLE_ROWS:
        lines.append(f"  ... and {len(ordered) - SAMPLE_ROWS} more")
    return "\n".join(lines)


def commit(path: str, account_id: str, rows: list[ParsedRow]) -> sync.ConnectionSync:
    conn = store.open_db()
    try:
        user_id = users.ensure_local_user(conn)
        source = build_source(path, account_id, user_id, rows=rows)
        return sync.sync_source(conn, user_id, source)
    finally:
        conn.close()


def import_csv(path: str, account_id: str) -> sync.ConnectionSync:
    return commit(path, account_id, parse(path, account_id))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import a bank CSV into one account.")
    parser.add_argument("file")
    parser.add_argument("--account", required=True, help="stable account identifier")
    parser.add_argument("--yes", action="store_true",
                        help="import without asking for confirmation")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass

    print(f"database  {safe(store.db_path())}")
    try:
        rows = parse(args.file, args.account)
    except Exception as exc:
        print(f"import FAILED: {type(exc).__name__}: {safe(exc)}", file=sys.stderr)
        print("nothing was written; the database was not opened.", file=sys.stderr)
        return 1

    print(preview(args.account, rows))
    if not args.yes:
        try:
            answer = input("Import these rows? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("cancelled -- nothing was written; the database was not opened.")
            return 1

    try:
        run = commit(args.file, args.account, rows)
    except Exception as exc:
        print(f"import FAILED while saving: {type(exc).__name__}: {safe(exc)}",
              file=sys.stderr)
        print("the save is a single transaction, so none of this file's rows "
              "were stored; the database file, schema and local user may "
              "already exist.", file=sys.stderr)
        return 1
    sync._report(run)
    print(f"  new       {run.result.new} transactions")
    print(f"  stored    {run.result.existing} transactions were already "
          f"stored for this account")
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    raise SystemExit(main())
