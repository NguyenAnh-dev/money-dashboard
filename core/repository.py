from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Any, NamedTuple, Sequence

from core.models import Account, Transaction

ACCOUNT_COLUMNS = (
    "id",
    "provider",
    "provider_id",
    "connection_id",
    "fetched_at",
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
    "fetched_at",
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


class SyncResult(NamedTuple):
    accounts: int
    transactions: int
    synced_at: str


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
        _iso(account.source.fetched_at),
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
        _iso(txn.source.fetched_at),
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


def save(
    conn: sqlite3.Connection,
    accounts: Sequence[Account],
    transactions: Sequence[Transaction],
    synced_at: datetime | None = None,
) -> SyncResult:
    accounts = list(accounts)
    transactions = list(transactions)
    stamp = (synced_at or datetime.now()).isoformat(timespec="seconds")

    with conn:
        save_accounts(conn, accounts)
        save_transactions(conn, transactions)
        conn.execute(
            "UPDATE meta SET last_synced_at = ? WHERE id = 1", (stamp,)
        )

    return SyncResult(len(accounts), len(transactions), stamp)


def last_synced_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT last_synced_at FROM meta WHERE id = 1").fetchone()
    return None if row is None else row["last_synced_at"]
