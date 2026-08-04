from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Any, NamedTuple, Sequence

from core.models import (
    Account,
    AccountType,
    Provider,
    SourceRef,
    Transaction,
    TransactionKind,
)

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


class SaveResult(NamedTuple):
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
    cursor: str | None = None,
) -> SaveResult:
    accounts = list(accounts)
    transactions = list(transactions)
    stamp = (synced_at or datetime.now()).isoformat(timespec="seconds")

    with conn:
        save_accounts(conn, accounts)
        save_transactions(conn, transactions)
        conn.execute(
            "UPDATE meta SET last_synced_at = ? WHERE id = 1", (stamp,)
        )
        if cursor is not None:
            conn.execute(
                "UPDATE meta SET sync_cursor = ? WHERE id = 1", (cursor,)
            )

    return SaveResult(len(accounts), len(transactions), stamp)


def sync_cursor(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT sync_cursor FROM meta WHERE id = 1").fetchone()
    return None if row is None else row["sync_cursor"]


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


def _source_ref(row: sqlite3.Row) -> SourceRef:
    return SourceRef(
        provider=Provider(row["provider"]),
        provider_id=row["provider_id"],
        connection_id=row["connection_id"],
        fetched_at=_to_datetime(row["fetched_at"]),
    )


def account_from_row(row: sqlite3.Row) -> Account:
    return Account(
        id=row["id"],
        source=_source_ref(row),
        name=row["name"],
        institution=row["institution"],
        type=AccountType(row["type"]),
        balance_cents=row["balance_cents"],
        currency=row["currency"],
        mask=row["mask"],
        available_cents=row["available_cents"],
        as_of=None if row["as_of"] is None else _to_date(row["as_of"]),
    )


def transaction_from_row(row: sqlite3.Row) -> Transaction:
    return Transaction(
        id=row["id"],
        source=_source_ref(row),
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
    rows = conn.execute(
        f"SELECT {', '.join(ACCOUNT_COLUMNS)} FROM accounts ORDER BY id"
    )
    return [account_from_row(row) for row in rows]


def load_transactions(conn: sqlite3.Connection) -> list[Transaction]:
    rows = conn.execute(
        f"SELECT {', '.join(TRANSACTION_COLUMNS)} FROM transactions "
        f"ORDER BY date, id"
    )
    return [transaction_from_row(row) for row in rows]
