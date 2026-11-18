from __future__ import annotations

import argparse
import sys

import sync
from adapters.csv_source import CONNECTION_ID, CsvAccount, CsvSource
from core import store, users
from core.models import scoped_connection_id


def validate(path: str, account_id: str) -> tuple[str, str]:
    if not path or not path.strip():
        raise ValueError("file argument must not be empty")
    account_id = account_id.strip()
    if not account_id:
        raise ValueError("account identifier must not be empty")
    return path, account_id


def build_source(path: str, account_id: str, user_id: str) -> CsvSource:
    path, account_id = validate(path, account_id)
    return CsvSource(
        path=path,
        account=CsvAccount(provider_id=account_id, name=account_id),
        connection_id=scoped_connection_id(f"{CONNECTION_ID}/{account_id}", user_id),
    )


def import_csv(path: str, account_id: str) -> sync.ConnectionSync:
    path, account_id = validate(path, account_id)
    conn = store.open_db()
    try:
        user_id = users.ensure_local_user(conn)
        source = build_source(path, account_id, user_id)
        return sync.sync_source(conn, user_id, source)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import a bank CSV into one account.")
    parser.add_argument("file")
    parser.add_argument("--account", required=True, help="stable account identifier")
    args = parser.parse_args(argv)

    print(f"database  {store.db_path()}")
    try:
        run = import_csv(args.file, args.account)
    except Exception as exc:
        print(f"import FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    sync._report(run)
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    raise SystemExit(main())
