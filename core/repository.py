from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Any, NamedTuple, Sequence

from core.models import (
    Account,
    AccountType,
    ConnectionRef,
    Provider,
    SourceRef,
    Transaction,
    TransactionKind,
    make_id,
)

ACCOUNT_COLUMNS = (
    "id",
    "provider",
    "provider_id",
    "connection_id",
    "name",
    "institution",
    "type",
    "balance_cents",
    "currency",
    "mask",
    "available_cents",
    "as_of",
)

TRANSACTION_COLUMNS = (
    "id",
    "provider",
    "provider_id",
    "connection_id",
    "account_id",
    "date",
    "name",
    "merchant",
    "amount_cents",
    "category",
    "currency",
    "kind",
    "pending",
    "raw",
)


class SaveResult(NamedTuple):
    accounts: int
    transactions: int
    synced_at: str
    deleted: int = 0
    unmatched: tuple[str, ...] = ()
    superseded: int = 0


class DeleteResult(NamedTuple):
    deleted: int
    unmatched: tuple[str, ...]


DELETE_CHUNK = 500

PENDING_TRANSACTION_KEY = "pending_transaction_id"


class RawEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return super().default(o)


def encode_raw(raw: dict) -> str:
    return json.dumps(raw, cls=RawEncoder, sort_keys=True)


def _iso(value: date | datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def account_row(account: Account) -> tuple:
    return (
        account.id,
        account.source.provider.value,
        account.source.provider_id,
        account.source.connection_id,
        account.name,
        account.institution,
        account.type.value,
        account.balance_cents,
        account.currency,
        account.mask,
        account.available_cents,
        _iso(account.as_of),
    )


def transaction_row(txn: Transaction) -> tuple:
    return (
        txn.id,
        txn.source.provider.value,
        txn.source.provider_id,
        txn.source.connection_id,
        txn.account_id,
        txn.date.isoformat(),
        txn.name,
        txn.merchant,
        txn.amount_cents,
        txn.category,
        txn.currency,
        txn.kind.value,
        int(txn.pending),
        encode_raw(txn.raw),
    )


def _upsert_sql(table: str, columns: Sequence[str]) -> str:
    placeholders = ", ".join("?" for _ in columns)
    assignments = ", ".join(f"{c} = excluded.{c}" for c in columns if c != "id")
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {assignments}"
    )


ACCOUNT_UPSERT = _upsert_sql("accounts", ACCOUNT_COLUMNS)
TRANSACTION_UPSERT = _upsert_sql("transactions", TRANSACTION_COLUMNS)


def save_accounts(conn: sqlite3.Connection, accounts: Sequence[Account]) -> int:
    conn.executemany(ACCOUNT_UPSERT, [account_row(a) for a in accounts])
    return len(accounts)


def save_transactions(
    conn: sqlite3.Connection, transactions: Sequence[Transaction]
) -> int:
    conn.executemany(
        TRANSACTION_UPSERT, [transaction_row(t) for t in transactions]
    )
    return len(transactions)


def delete_transactions(
    conn: sqlite3.Connection, ids: Sequence[str]
) -> DeleteResult:
    ids = list(ids)
    if not ids:
        return DeleteResult(0, ())

    present: set[str] = set()
    for start in range(0, len(ids), DELETE_CHUNK):
        chunk = ids[start:start + DELETE_CHUNK]
        placeholders = ", ".join("?" for _ in chunk)
        present.update(
            row["id"] for row in conn.execute(
                f"SELECT id FROM transactions WHERE id IN ({placeholders})",
                chunk,
            )
        )

    conn.executemany(
        "DELETE FROM transactions WHERE id = ?", [(i,) for i in ids]
    )

    return DeleteResult(
        deleted=len(present),
        unmatched=tuple(i for i in ids if i not in present),
    )


def superseded_id(txn: Transaction) -> str | None:
    prior = txn.raw.get(PENDING_TRANSACTION_KEY)
    if not prior:
        return None

    replaced = make_id(SourceRef(
        provider=txn.source.provider,
        provider_id=prior,
        connection_id=txn.source.connection_id,
    ))
    return None if replaced == txn.id else replaced


def superseded_ids(transactions: Sequence[Transaction]) -> list[str]:
    found = [superseded_id(txn) for txn in transactions]
    return [i for i in found if i is not None]


def save(
    conn: sqlite3.Connection,
    accounts: Sequence[Account],
    transactions: Sequence[Transaction],
    synced_at: datetime | None = None,
    cursor: str | None = None,
    connection: ConnectionRef | None = None,
    removed: Sequence[str] = (),
) -> SaveResult:
    accounts = list(accounts)
    transactions = list(transactions)
    when = (synced_at or datetime.now()).replace(microsecond=0)
    stamp = when.isoformat(timespec="seconds")

    with conn:
        save_accounts(conn, accounts)
        save_transactions(conn, transactions)
        deletion = delete_transactions(conn, removed)
        settled = delete_transactions(conn, superseded_ids(transactions))
        conn.execute(
            "UPDATE meta SET last_synced_at = ? WHERE id = 1", (stamp,)
        )
        if connection is not None:
            save_connection(conn, connection, cursor=cursor, fetched_at=when)

    return SaveResult(
        len(accounts),
        len(transactions),
        stamp,
        deletion.deleted,
        deletion.unmatched,
        settled.deleted,
    )


def save_connection(
    conn: sqlite3.Connection,
    connection: ConnectionRef,
    cursor: str | None = None,
    fetched_at: datetime | None = None,
) -> None:
    conn.execute(
        "INSERT INTO connections (provider, connection_id, cursor, fetched_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(provider, connection_id) DO UPDATE SET "
        "cursor = COALESCE(excluded.cursor, connections.cursor), "
        "fetched_at = excluded.fetched_at",
        (
            connection.provider.value,
            connection.connection_id,
            cursor,
            _iso(fetched_at),
        ),
    )


def cursor_for(conn: sqlite3.Connection, connection: ConnectionRef) -> str | None:
    row = conn.execute(
        "SELECT cursor FROM connections WHERE provider = ? AND connection_id = ?",
        (connection.provider.value, connection.connection_id),
    ).fetchone()
    return None if row is None else row["cursor"]


def fetched_at_for(
    conn: sqlite3.Connection, connection: ConnectionRef
) -> datetime | None:
    row = conn.execute(
        "SELECT fetched_at FROM connections "
        "WHERE provider = ? AND connection_id = ?",
        (connection.provider.value, connection.connection_id),
    ).fetchone()
    return None if row is None else _to_datetime(row["fetched_at"])


def load_connections(conn: sqlite3.Connection) -> dict[tuple[str, str], datetime | None]:
    return {
        (row["provider"], row["connection_id"]): _to_datetime(row["fetched_at"])
        for row in conn.execute(
            "SELECT provider, connection_id, fetched_at FROM connections"
        )
    }


def last_synced_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT last_synced_at FROM meta WHERE id = 1").fetchone()
    return None if row is None else row["last_synced_at"]


def stored_providers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT provider FROM accounts ORDER BY provider")
    return [row["provider"] for row in rows]


def decode_raw(text: str) -> dict:
    return json.loads(text)


def _to_date(value: str) -> date:
    return date.fromisoformat(value)


def _to_datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _source_ref(row: sqlite3.Row, fetched_at: datetime | None = None) -> SourceRef:
    return SourceRef(
        provider=Provider(row["provider"]),
        provider_id=row["provider_id"],
        connection_id=row["connection_id"],
        fetched_at=fetched_at,
    )


def account_from_row(
    row: sqlite3.Row, fetched_at: datetime | None = None
) -> Account:
    return Account(
        id=row["id"],
        source=_source_ref(row, fetched_at),
        name=row["name"],
        institution=row["institution"],
        type=AccountType(row["type"]),
        balance_cents=row["balance_cents"],
        currency=row["currency"],
        mask=row["mask"],
        available_cents=row["available_cents"],
        as_of=None if row["as_of"] is None else _to_date(row["as_of"]),
    )


def transaction_from_row(
    row: sqlite3.Row, fetched_at: datetime | None = None
) -> Transaction:
    return Transaction(
        id=row["id"],
        source=_source_ref(row, fetched_at),
        account_id=row["account_id"],
        date=_to_date(row["date"]),
        name=row["name"],
        merchant=row["merchant"],
        amount_cents=row["amount_cents"],
        category=row["category"],
        currency=row["currency"],
        kind=TransactionKind(row["kind"]),
        pending=bool(row["pending"]),
        raw=decode_raw(row["raw"]),
    )


def load_accounts(conn: sqlite3.Connection) -> list[Account]:
    fetched = load_connections(conn)
    rows = conn.execute(
        f"SELECT {', '.join(ACCOUNT_COLUMNS)} FROM accounts ORDER BY id"
    )
    return [
        account_from_row(
            row, fetched.get((row["provider"], row["connection_id"]))
        )
        for row in rows
    ]


def load_transactions(conn: sqlite3.Connection) -> list[Transaction]:
    fetched = load_connections(conn)
    rows = conn.execute(
        f"SELECT {', '.join(TRANSACTION_COLUMNS)} FROM transactions "
        f"ORDER BY date, id"
    )
    return [
        transaction_from_row(
            row, fetched.get((row["provider"], row["connection_id"]))
        )
        for row in rows
    ]
