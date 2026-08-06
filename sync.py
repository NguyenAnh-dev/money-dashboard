from __future__ import annotations

import sys
from typing import NamedTuple

from core import repository, store, users
from core.models import ConnectionRef
from data_source import active_source_name, get_source


class ConnectionSync(NamedTuple):
    connection: ConnectionRef
    result: repository.SaveResult


def linked_sources(conn, user_id: str) -> list:
    from adapters.plaid_source import PlaidSource

    return [
        PlaidSource(access_token=token, item_id=ref.connection_id)
        for ref, token in repository.linked_connections(conn, user_id)
    ]


def sources_for(conn, user_id: str, include_configured: bool = True) -> list:
    sources = linked_sources(conn, user_id)
    if not include_configured:
        return sources

    known = {source.connection() for source in sources}
    try:
        configured = get_source(user_id)
    except Exception as exc:
        if not sources:
            raise
        print(f"skipping DATA_SOURCE={active_source_name()}: {exc}",
              file=sys.stderr)
        return sources

    if configured.connection() not in known:
        sources.append(configured)
    return sources


def sync_source(conn, user_id: str, source) -> ConnectionSync:
    connection = source.connection()

    result = source.fetch_transactions(
        repository.cursor_for(conn, user_id, connection)
    )
    accounts = source.fetch_accounts()

    saved = repository.save(
        conn,
        user_id,
        accounts,
        [*result.added, *result.modified],
        cursor=result.cursor,
        connection=connection,
        removed=result.removed,
    )
    return ConnectionSync(connection, saved)


def sync(user_id: str | None = None,
         include_configured: bool = True) -> list[ConnectionSync]:
    conn = store.open_db()
    try:
        if user_id is None:
            user_id = users.ensure_local_user(conn)

        return [
            sync_source(conn, user_id, source)
            for source in sources_for(conn, user_id, include_configured)
        ]
    finally:
        conn.close()


def _report(run: ConnectionSync) -> None:
    result = run.result
    label = f"{run.connection.provider.value}:{run.connection.connection_id}"

    print(f"\n{label}")
    print(f"  wrote     {result.accounts} accounts, "
          f"{result.transactions} transactions")
    if result.deleted:
        print(f"  deleted   {result.deleted} transactions")
    if result.superseded:
        print(f"  settled   {result.superseded} pending transactions replaced")
    if result.unmatched:
        print(f"\nWARNING: {len(result.unmatched)} removal(s) matched no "
              f"stored row on {label}:", file=sys.stderr)
        for rid in result.unmatched[:5]:
            print(f"    {rid}", file=sys.stderr)
        if len(result.unmatched) > 5:
            print(f"    ... and {len(result.unmatched) - 5} more",
                  file=sys.stderr)
        print("The source withdrew a transaction this database never held. "
              "Usually harmless (it arrived and was withdrawn between syncs), "
              "but a persistent count means ids are being minted differently "
              "on the two sides.", file=sys.stderr)
    print(f"  synced    {result.synced_at}")


def main() -> int:
    print(f"source    {active_source_name()}")
    print(f"database  {store.db_path()}")

    try:
        runs = sync()
    except Exception as exc:
        print(f"\nsync FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("nothing was written -- the database is unchanged.", file=sys.stderr)
        raise

    for run in runs:
        _report(run)

    if len(runs) > 1:
        print(f"\ntotal     {sum(r.result.accounts for r in runs)} accounts, "
              f"{sum(r.result.transactions for r in runs)} transactions "
              f"across {len(runs)} connections")
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    raise SystemExit(main())
