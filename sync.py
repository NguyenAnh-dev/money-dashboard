from __future__ import annotations

import sys

from core import repository, store
from data_source import active_source_name, get_source


def sync() -> repository.SaveResult:
    source = get_source()
    connection = source.connection()

    conn = store.open_db()
    try:
        result = source.fetch_transactions(repository.cursor_for(conn, connection))
        accounts = source.fetch_accounts()

        return repository.save(
            conn,
            accounts,
            [*result.added, *result.modified],
            cursor=result.cursor,
            connection=connection,
            removed=result.removed,
        )
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
    if result.deleted:
        print(f"deleted   {result.deleted} transactions")
    if result.superseded:
        print(f"settled   {result.superseded} pending transactions replaced")
    if result.unmatched:
        print(f"\nWARNING: {len(result.unmatched)} removal(s) matched no "
              f"stored row:", file=sys.stderr)
        for rid in result.unmatched[:5]:
            print(f"    {rid}", file=sys.stderr)
        if len(result.unmatched) > 5:
            print(f"    ... and {len(result.unmatched) - 5} more",
                  file=sys.stderr)
        print("The source withdrew a transaction this database never held. "
              "Usually harmless (it arrived and was withdrawn between syncs), "
              "but a persistent count means ids are being minted differently "
              "on the two sides.", file=sys.stderr)
    print(f"synced    {result.synced_at}")
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    raise SystemExit(main())
