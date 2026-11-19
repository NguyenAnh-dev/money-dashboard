from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Any, NamedTuple, Sequence

from core import crypto
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
    new: int = 0
    existing: int = 0


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


def connection_pairs(records: Sequence[Account | Transaction]) -> set[tuple[str, str]]:
    return {
        (r.source.provider.value, r.source.connection_id) for r in records
    }


def ensure_connections(
    conn: sqlite3.Connection,
    user_id: str,
    pairs: set[tuple[str, str]],
) -> None:
    for provider, connection_id in sorted(pairs):
        row = conn.execute(
            "SELECT user_id FROM connections "
            "WHERE provider = ? AND connection_id = ?",
            (provider, connection_id),
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO connections (provider, connection_id, user_id) "
                "VALUES (?, ?, ?)",
                (provider, connection_id, user_id),
            )
        elif row["user_id"] != user_id:
            raise ValueError(
                f"connection {provider}:{connection_id} belongs to user "
                f"{row['user_id']}, not {user_id}. Every id this connection "
                f"mints is namespaced on that pair and carries no user "
                f"segment, so writing it under a second user would overwrite "
                f"the first user's rows rather than sit beside them."
            )


def save_accounts(
    conn: sqlite3.Connection, user_id: str, accounts: Sequence[Account]
) -> int:
    ensure_connections(conn, user_id, connection_pairs(accounts))
    conn.executemany(ACCOUNT_UPSERT, [account_row(a) for a in accounts])
    return len(accounts)


def save_transactions(
    conn: sqlite3.Connection, user_id: str, transactions: Sequence[Transaction]
) -> int:
    ensure_connections(conn, user_id, connection_pairs(transactions))
    conn.executemany(
        TRANSACTION_UPSERT, [transaction_row(t) for t in transactions]
    )
    return len(transactions)


def count_stored(
    conn: sqlite3.Connection, transactions: Sequence[Transaction]
) -> int:
    ids = sorted({t.id for t in transactions})
    found = 0
    for start in range(0, len(ids), DELETE_CHUNK):
        chunk = ids[start:start + DELETE_CHUNK]
        placeholders = ", ".join("?" for _ in chunk)
        found += conn.execute(
            f"SELECT COUNT(*) FROM transactions WHERE id IN ({placeholders})",
            chunk,
        ).fetchone()[0]
    return found


def delete_transactions(
    conn: sqlite3.Connection, user_id: str, ids: Sequence[str]
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
                f"SELECT t.id FROM transactions t "
                f"JOIN connections c "
                f"  ON c.provider = t.provider "
                f" AND c.connection_id = t.connection_id "
                f"WHERE c.user_id = ? AND t.id IN ({placeholders})",
                (user_id, *chunk),
            )
        )

    conn.executemany(
        "DELETE FROM transactions WHERE id = ?", [(i,) for i in sorted(present)]
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
    user_id: str,
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
        if connection is not None:
            save_connection(conn, user_id, connection, cursor=cursor,
                            fetched_at=when)
        save_accounts(conn, user_id, accounts)
        existing = count_stored(conn, transactions)
        save_transactions(conn, user_id, transactions)
        deletion = delete_transactions(conn, user_id, removed)
        settled = delete_transactions(
            conn, user_id, superseded_ids(transactions)
        )
        conn.execute(
            "UPDATE meta SET last_synced_at = ? WHERE id = 1", (stamp,)
        )

    return SaveResult(
        len(accounts),
        len(transactions),
        stamp,
        deletion.deleted,
        deletion.unmatched,
        settled.deleted,
        len({t.id for t in transactions}) - existing,
        existing,
    )


def save_connection(
    conn: sqlite3.Connection,
    user_id: str,
    connection: ConnectionRef,
    cursor: str | None = None,
    fetched_at: datetime | None = None,
) -> None:
    ensure_connections(
        conn,
        user_id,
        {(connection.provider.value, connection.connection_id)},
    )
    conn.execute(
        "UPDATE connections SET "
        "cursor = COALESCE(?, cursor), "
        "fetched_at = ? "
        "WHERE provider = ? AND connection_id = ? AND user_id = ?",
        (
            cursor,
            _iso(fetched_at),
            connection.provider.value,
            connection.connection_id,
            user_id,
        ),
    )


def save_access_token(
    conn: sqlite3.Connection,
    user_id: str,
    connection: ConnectionRef,
    access_token: str,
) -> None:
    ensure_connections(
        conn,
        user_id,
        {(connection.provider.value, connection.connection_id)},
    )
    updated = conn.execute(
        "UPDATE connections SET access_token = ? "
        "WHERE provider = ? AND connection_id = ? AND user_id = ?",
        (
            crypto.encrypt(access_token),
            connection.provider.value,
            connection.connection_id,
            user_id,
        ),
    ).rowcount

    if not updated:
        raise ValueError(
            f"no connection {connection.provider.value}:"
            f"{connection.connection_id} for user {user_id}"
        )


def access_token_for(
    conn: sqlite3.Connection, user_id: str, connection: ConnectionRef
) -> str | None:
    row = conn.execute(
        "SELECT access_token FROM connections "
        "WHERE provider = ? AND connection_id = ? AND user_id = ?",
        (connection.provider.value, connection.connection_id, user_id),
    ).fetchone()

    if row is None or row["access_token"] is None:
        return None
    return crypto.decrypt(row["access_token"])


def connections_for(
    conn: sqlite3.Connection, user_id: str
) -> list[ConnectionRef]:
    return [
        ConnectionRef(
            provider=Provider(row["provider"]),
            connection_id=row["connection_id"],
        )
        for row in conn.execute(
            "SELECT provider, connection_id FROM connections "
            "WHERE user_id = ? ORDER BY provider, connection_id",
            (user_id,),
        )
    ]


def linked_connections(
    conn: sqlite3.Connection, user_id: str
) -> list[tuple[ConnectionRef, str]]:
    return [
        (
            ConnectionRef(
                provider=Provider(row["provider"]),
                connection_id=row["connection_id"],
            ),
            crypto.decrypt(row["access_token"]),
        )
        for row in conn.execute(
            "SELECT provider, connection_id, access_token FROM connections "
            "WHERE user_id = ? AND access_token IS NOT NULL "
            "ORDER BY provider, connection_id",
            (user_id,),
        )
    ]


def owner_of(
    conn: sqlite3.Connection, connection: ConnectionRef
) -> str | None:
    row = conn.execute(
        "SELECT user_id FROM connections "
        "WHERE provider = ? AND connection_id = ?",
        (connection.provider.value, connection.connection_id),
    ).fetchone()
    return None if row is None else row["user_id"]


def cursor_for(
    conn: sqlite3.Connection, user_id: str, connection: ConnectionRef
) -> str | None:
    row = conn.execute(
        "SELECT cursor FROM connections "
        "WHERE provider = ? AND connection_id = ? AND user_id = ?",
        (connection.provider.value, connection.connection_id, user_id),
    ).fetchone()
    return None if row is None else row["cursor"]


def fetched_at_for(
    conn: sqlite3.Connection, user_id: str, connection: ConnectionRef
) -> datetime | None:
    row = conn.execute(
        "SELECT fetched_at FROM connections "
        "WHERE provider = ? AND connection_id = ? AND user_id = ?",
        (connection.provider.value, connection.connection_id, user_id),
    ).fetchone()
    return None if row is None else _to_datetime(row["fetched_at"])


def load_connections(
    conn: sqlite3.Connection, user_id: str
) -> dict[tuple[str, str], datetime | None]:
    return {
        (row["provider"], row["connection_id"]): _to_datetime(row["fetched_at"])
        for row in conn.execute(
            "SELECT provider, connection_id, fetched_at FROM connections "
            "WHERE user_id = ?",
            (user_id,),
        )
    }


def last_synced_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT last_synced_at FROM meta WHERE id = 1").fetchone()
    return None if row is None else row["last_synced_at"]


def stored_providers(conn: sqlite3.Connection, user_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT a.provider FROM accounts a "
        "JOIN connections c "
        "  ON c.provider = a.provider AND c.connection_id = a.connection_id "
        "WHERE c.user_id = ? "
        "ORDER BY a.provider",
        (user_id,),
    )
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


def _owned_by(table: str, columns: Sequence[str], order_by: str) -> str:
    selected = ", ".join(f"t.{c}" for c in columns)
    return (
        f"SELECT {selected} FROM {table} t "
        f"JOIN connections c "
        f"  ON c.provider = t.provider AND c.connection_id = t.connection_id "
        f"WHERE c.user_id = ? "
        f"ORDER BY {order_by}"
    )


ACCOUNT_SELECT = _owned_by("accounts", ACCOUNT_COLUMNS, "t.id")
TRANSACTION_SELECT = _owned_by("transactions", TRANSACTION_COLUMNS, "t.date, t.id")


def load_accounts(conn: sqlite3.Connection, user_id: str) -> list[Account]:
    fetched = load_connections(conn, user_id)
    rows = conn.execute(ACCOUNT_SELECT, (user_id,))
    return [
        account_from_row(
            row, fetched.get((row["provider"], row["connection_id"]))
        )
        for row in rows
    ]


def load_transactions(
    conn: sqlite3.Connection, user_id: str
) -> list[Transaction]:
    fetched = load_connections(conn, user_id)
    rows = conn.execute(TRANSACTION_SELECT, (user_id,))
    return [
        transaction_from_row(
            row, fetched.get((row["provider"], row["connection_id"]))
        )
        for row in rows
    ]


TRACKED = "tracked"
DISMISSED = "dismissed"


class UnknownAccount(ValueError):
    pass


class DecisionDismissed(ValueError):
    pass


def owns_account(conn: sqlite3.Connection, user_id: str, account_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM accounts a JOIN connections c "
        "ON c.provider = a.provider AND c.connection_id = a.connection_id "
        "WHERE a.id = ? AND c.user_id = ?",
        (account_id, user_id),
    ).fetchone()
    return row is not None


def set_decision(
    conn: sqlite3.Connection,
    user_id: str,
    account_id: str,
    merchant: str,
    decision: str,
    decided_at: datetime | None = None,
) -> None:
    if not owns_account(conn, user_id, account_id):
        raise UnknownAccount(account_id)
    stamp = (decided_at or datetime.now()).replace(microsecond=0).isoformat()
    guard = " WHERE subscription_decisions.decision <> 'dismissed'"         if decision == TRACKED else ""
    with conn:
        cursor = conn.execute(
            "INSERT INTO subscription_decisions "
            "(user_id, account_id, merchant, decision, decided_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, account_id, merchant) DO UPDATE SET "
            "decision = excluded.decision, decided_at = excluded.decided_at"
            + guard,
            (user_id, account_id, merchant, decision, stamp),
        )
    if cursor.rowcount == 0:
        raise DecisionDismissed(merchant)


def clear_decision(
    conn: sqlite3.Connection, user_id: str, account_id: str, merchant: str
) -> bool:
    with conn:
        cursor = conn.execute(
            "DELETE FROM subscription_decisions "
            "WHERE user_id = ? AND account_id = ? AND merchant = ?",
            (user_id, account_id, merchant),
        )
    return cursor.rowcount > 0


def load_decisions(
    conn: sqlite3.Connection, user_id: str
) -> dict[tuple[str, str], str]:
    rows = conn.execute(
        "SELECT account_id, merchant, decision FROM subscription_decisions "
        "WHERE user_id = ?",
        (user_id,),
    )
    return {(row["account_id"], row["merchant"]): row["decision"] for row in rows}
