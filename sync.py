from __future__ import annotations

import sys

from core import repository, store
from data_source import active_source_name, get_source


def sync() -> repository.SyncResult:
    source = get_source()
    transactions = source.fetch_transactions()
    accounts = source.fetch_accounts()

    conn = store.open_db()
    try:
        return repository.save(conn, accounts, transactions)
    finally:
        conn.close()


def main() -> int:
    source = active_source_name()
    path = store.db_path()

    print(f"source    {source}")
    print(f"database  {path}")

    try:
        result = sync()
    except Exception as exc:
        print(f"\nsync FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("nothing was written -- the database is unchanged.", file=sys.stderr)
        raise

    print(f"wrote     {result.accounts} accounts, "
          f"{result.transactions} transactions")
    print(f"synced    {result.synced_at}")
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    raise SystemExit(main())
