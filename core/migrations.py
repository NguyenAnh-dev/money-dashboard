from __future__ import annotations

import sqlite3
from typing import Callable

from core.models import Provider, SourceRef, make_id, scoped_connection_id

EARLIEST_SUPPORTED_VERSION = 6

USER_SCOPED_PROVIDERS = (Provider.FAKE.value, Provider.CSV.value)


class UnsupportedSchemaVersion(RuntimeError):
    def __init__(self, found: int, path: str) -> None:
        super().__init__(
            f"database schema version {found} is older than the earliest this "
            f"code can migrate from, {EARLIEST_SUPPORTED_VERSION} ({path}). "
            f"Versions 1-{EARLIEST_SUPPORTED_VERSION - 1} predate the migration "
            f"path and no upgrade from them was ever written -- nobody outside "
            f"this repository has such a file. Delete it and re-sync. Note that "
            f"a file this old cannot hold a linked Plaid connection, so a "
            f"re-sync loses nothing that a browser and a human are needed to "
            f"rebuild."
        )
        self.found = found


class MigrationFailed(RuntimeError):
    pass


def stored_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT schema_version FROM meta WHERE id = 1"
    ).fetchone()
    return None if row is None else row["schema_version"]


def pending(found: int, target: int) -> list[int]:
    return list(range(found, target))


def upgrade(conn: sqlite3.Connection, target: int, path: str = "") -> list[int]:
    found = stored_version(conn)
    if found is None or found >= target:
        return []

    if found < EARLIEST_SUPPORTED_VERSION:
        raise UnsupportedSchemaVersion(found, path)

    steps = pending(found, target)
    missing = [version for version in steps if version not in MIGRATIONS]
    if missing:
        raise MigrationFailed(
            f"no migration is registered for schema version(s) {missing} -> "
            f"{[v + 1 for v in missing]}. SCHEMA_VERSION was bumped without "
            f"writing the step that gets an existing file there, so every "
            f"database written before the bump is now unopenable."
        )

    applied = []
    for version in steps:
        _apply(conn, version, MIGRATIONS[version])
        applied.append(version + 1)
    return applied


def _apply(
    conn: sqlite3.Connection,
    version: int,
    step: Callable[[sqlite3.Connection], None],
) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:
            step(conn)
            _require_referential_integrity(conn, version)
            conn.execute(
                "UPDATE meta SET schema_version = ? WHERE id = 1",
                (version + 1,),
            )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _require_referential_integrity(
    conn: sqlite3.Connection, version: int
) -> None:
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if not violations:
        return

    sample = [tuple(row) for row in violations[:3]]
    raise MigrationFailed(
        f"migration {version} -> {version + 1} left {len(violations)} row(s) "
        f"pointing at a parent that does not exist: {sample}. Foreign keys are "
        f"off while a migration runs -- rewriting a primary key is impossible "
        f"with them on -- so this check is the only thing standing between a "
        f"half-rekeyed file and a dashboard quietly missing an account. "
        f"Rolled back; the database is unchanged."
    )


def to_v7(conn: sqlite3.Connection) -> None:
    placeholders = ", ".join("?" for _ in USER_SCOPED_PROVIDERS)
    rows = conn.execute(
        f"SELECT provider, connection_id, user_id FROM connections "
        f"WHERE provider IN ({placeholders}) "
        f"ORDER BY provider, connection_id",
        USER_SCOPED_PROVIDERS,
    ).fetchall()

    for row in rows:
        _rekey_connection(
            conn, row["provider"], row["connection_id"], row["user_id"]
        )


def _rekey_connection(
    conn: sqlite3.Connection, provider: str, old: str, user_id: str
) -> None:
    new = scoped_connection_id(old, user_id)
    if new == old or old.endswith(f":{user_id}"):
        return

    taken = conn.execute(
        "SELECT user_id FROM connections WHERE provider = ? AND connection_id = ?",
        (provider, new),
    ).fetchone()
    if taken is not None:
        raise MigrationFailed(
            f"cannot rekey {provider}:{old} to {provider}:{new} -- that "
            f"connection already exists and belongs to {taken['user_id']}. "
            f"Two rows would have to merge, and an upsert picking a winner is "
            f"exactly the silent overwrite the scoping exists to prevent."
        )

    accounts = conn.execute(
        "SELECT id, provider_id FROM accounts "
        "WHERE provider = ? AND connection_id = ?",
        (provider, old),
    ).fetchall()
    transactions = conn.execute(
        "SELECT id, provider_id, account_id FROM transactions "
        "WHERE provider = ? AND connection_id = ?",
        (provider, old),
    ).fetchall()

    rekeyed = {
        row["id"]: _id_under(provider, new, row["provider_id"])
        for row in accounts
    }

    conn.execute(
        "UPDATE connections SET connection_id = ? "
        "WHERE provider = ? AND connection_id = ?",
        (new, provider, old),
    )
    conn.executemany(
        "UPDATE accounts SET id = ?, connection_id = ? WHERE id = ?",
        [(rekeyed[row["id"]], new, row["id"]) for row in accounts],
    )
    conn.executemany(
        "UPDATE transactions SET id = ?, connection_id = ?, account_id = ? "
        "WHERE id = ?",
        [
            (
                _id_under(provider, new, row["provider_id"]),
                new,
                rekeyed.get(row["account_id"], row["account_id"]),
                row["id"],
            )
            for row in transactions
        ],
    )


def _id_under(provider: str, connection_id: str, provider_id: str) -> str:
    return make_id(SourceRef(
        provider=Provider(provider),
        provider_id=provider_id,
        connection_id=connection_id,
    ))


def to_v8(conn: sqlite3.Connection) -> None:
    return None


def to_v9(conn: sqlite3.Connection) -> None:
    tracked = conn.execute(
        "SELECT user_id, account_id, merchant FROM subscription_decisions "
        "WHERE decision = 'tracked'"
    ).fetchall()
    for user_id, account_id, merchant in tracked:
        charges = conn.execute(
            "SELECT id FROM transactions WHERE account_id = ? AND merchant = ? "
            "AND pending = 0 AND amount_cents > 0 AND kind <> 'transfer' "
            "ORDER BY date, rowid",
            (account_id, merchant),
        ).fetchall()
        conn.executemany(
            "INSERT OR IGNORE INTO subscription_price_observed "
            "(user_id, account_id, merchant, charge_id, seq) "
            "VALUES (?, ?, ?, ?, ?)",
            [(user_id, account_id, merchant, row[0], seq)
             for seq, row in enumerate(charges, start=1)],
        )


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def to_v10(conn: sqlite3.Connection) -> None:
    eligible = (
        "SELECT id, date, amount_cents FROM transactions WHERE account_id = ? "
        "AND merchant = ? AND pending = 0 AND amount_cents > 0 "
        "AND kind <> 'transfer' ORDER BY date, rowid"
    )
    tracked = conn.execute(
        "SELECT user_id, account_id, merchant FROM subscription_decisions "
        "WHERE decision = 'tracked'"
    ).fetchall()
    for user_id, account_id, merchant in tracked:
        key = (user_id, account_id, merchant)
        watch = conn.execute(
            "SELECT scanned_through FROM subscription_price_watch "
            "WHERE user_id = ? AND account_id = ? AND merchant = ?", key
        ).fetchone()
        seen = conn.execute(
            "SELECT count(*) FROM subscription_price_observed "
            "WHERE user_id = ? AND account_id = ? AND merchant = ?", key
        ).fetchone()[0]
        if watch is not None and seen == 0:
            charges = [
                row for row in conn.execute(eligible, (account_id, merchant))
                if row[1] <= watch[0]
            ]
            conn.executemany(
                "INSERT OR IGNORE INTO subscription_price_observed "
                "(user_id, account_id, merchant, charge_id, seq) "
                "VALUES (?, ?, ?, ?, ?)",
                [(*key, row[0], seq) for seq, row in enumerate(charges, start=1)],
            )
        elif watch is None and seen:
            newest = conn.execute(
                "SELECT max(t.date) FROM subscription_price_observed o "
                "JOIN transactions t ON t.id = o.charge_id "
                "WHERE o.user_id = ? AND o.account_id = ? AND o.merchant = ?", key
            ).fetchone()[0]
            if newest is not None:
                conn.execute(
                    "INSERT INTO subscription_price_watch "
                    "(user_id, account_id, merchant, scanned_through) "
                    "VALUES (?, ?, ?, ?)", (*key, newest),
                )

    has_old = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'subscription_price_events'"
    ).fetchone()
    if not has_old:
        return
    with_previous = "previous_charge_id" in _table_columns(
        conn, "subscription_price_events")
    for row in conn.execute(
        "SELECT * FROM subscription_price_events"
    ).fetchall():
        user_id, account_id, merchant = row["user_id"], row["account_id"], row["merchant"]
        previous_id = None
        if with_previous:
            previous_id = row["previous_charge_id"]
        else:
            charges = conn.execute(eligible, (account_id, merchant)).fetchall()
            ids = [c[0] for c in charges]
            if row["charge_id"] in ids and ids.index(row["charge_id"]) > 0:
                at = ids.index(row["charge_id"])
                before, now = charges[at - 1], charges[at]
                if (before[2] == row["old_cents"] and now[2] == row["new_cents"]
                        and now[1] == row["charge_date"]):
                    previous_id = before[0]
        if previous_id is None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO subscription_price_increases "
            "(user_id, account_id, merchant, charge_id, previous_charge_id, "
            "charge_date, old_cents, new_cents) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, account_id, merchant, row["charge_id"], previous_id,
             row["charge_date"], row["old_cents"], row["new_cents"]),
        )


def to_v11(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO subscription_price_corrected "
        "(user_id, account_id, merchant, charge_id) "
        "SELECT e.user_id, e.account_id, e.merchant, e.previous_charge_id "
        "FROM subscription_price_increases e "
        "JOIN subscription_price_observed oi "
        "  ON oi.user_id = e.user_id AND oi.account_id = e.account_id "
        " AND oi.merchant = e.merchant AND oi.charge_id = e.charge_id "
        "JOIN subscription_price_observed op "
        "  ON op.user_id = e.user_id AND op.account_id = e.account_id "
        " AND op.merchant = e.merchant AND op.charge_id = e.previous_charge_id "
        "WHERE op.seq > oi.seq"
    )


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    6: to_v7,
    7: to_v8,
    8: to_v9,
    9: to_v10,
    10: to_v11,
}
