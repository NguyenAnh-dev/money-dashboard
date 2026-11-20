from __future__ import annotations

import json
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
        if _apply(conn, version, MIGRATIONS[version]):
            applied.append(version + 1)
    return applied


def _apply(
    conn: sqlite3.Connection,
    version: int,
    step: Callable[[sqlite3.Connection], None],
) -> bool:
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if stored_version(conn) != version:
                return False
            step(conn)
            _require_referential_integrity(conn, version)
            conn.execute(
                "UPDATE meta SET schema_version = ? WHERE id = 1",
                (version + 1,),
            )
        return True
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


_LEGACY_NETFLIX_PREFIX = "NETFLIX"
_NETFLIX_MERCHANT = "Netflix"
_MERCHANT_KEYED = (
    "subscription_price_increases",
    "subscription_price_corrected",
    "subscription_price_acknowledged",
)


def _provider_named(provider: str, raw_json: str) -> bool:
    if provider != Provider.PLAID.value:
        return False
    try:
        payload = json.loads(raw_json)
    except (TypeError, ValueError):
        return False
    name = payload.get("merchant_name") if isinstance(payload, dict) else None
    return isinstance(name, str) and bool(name.strip())


def _legacy_fallback(description: str) -> str:
    return " ".join(description.strip().split()[:2]).title()


def _ledger_with_position(conn, keys, merchant):
    out = []
    last = 0
    for row in conn.execute(
        "SELECT o.charge_id, t.rowid AS position "
        "FROM subscription_price_observed o "
        "LEFT JOIN transactions t ON t.id = o.charge_id "
        "WHERE o.user_id = ? AND o.account_id = ? AND o.merchant = ? "
        "ORDER BY o.seq", (*keys, merchant)
    ):
        last = row["position"] if row["position"] is not None else last
        out.append((row["charge_id"], last))
    return out


def _merge_ledgers(ledgers):
    heads = {m: 0 for m in ledgers}
    merged, placed = [], set()
    while True:
        live = [(ledgers[m][heads[m]][1], m) for m in sorted(ledgers)
                if heads[m] < len(ledgers[m])]
        if not live:
            return merged
        _, merchant = min(live)
        charge = ledgers[merchant][heads[merchant]][0]
        heads[merchant] += 1
        if charge not in placed:
            placed.add(charge)
            merged.append(charge)


def _corrected_under(conn, keys, merchant):
    return {row[0] for row in conn.execute(
        "SELECT charge_id FROM subscription_price_corrected WHERE user_id = ? "
        "AND account_id = ? AND merchant = ?", (*keys, merchant))}


def _snapshot_group_histories(conn, keys, group, ledgers, corrected):
    if sum(1 for m in group if ledgers[m]) < 2:
        return
    for merchant in group:
        order = [charge for charge, _ in ledgers[merchant]]
        foreign = sorted(set().union(
            *(corrected[o] for o in group if o != merchant)) - corrected[merchant])
        for row in conn.execute(
            "SELECT charge_id FROM subscription_price_increases WHERE "
            "user_id = ? AND account_id = ? AND merchant = ? AND history IS NULL",
            (*keys, merchant)
        ).fetchall():
            if row["charge_id"] not in order:
                continue
            seen = order[:order.index(row["charge_id"]) + 1]
            conn.execute(
                "UPDATE subscription_price_increases SET history = ? WHERE "
                "user_id = ? AND account_id = ? AND merchant = ? AND "
                "charge_id = ?",
                (json.dumps({"seen": seen, "foreign": foreign}), *keys,
                 merchant, row["charge_id"]))


def _rekey_group(
    conn: sqlite3.Connection, account_id: str, aliases: list, new: str
) -> None:
    group = [new] + sorted(aliases)
    marks = ", ".join("?" for _ in group)
    users = sorted({row[0] for table in (
        "subscription_decisions", "subscription_price_observed",
        "subscription_price_acknowledged", "subscription_price_watch",
        "subscription_price_increases", "subscription_price_corrected")
        for row in conn.execute(
            f"SELECT DISTINCT user_id FROM {table} WHERE account_id = ? "
            f"AND merchant IN ({marks})", (account_id, *group))})
    for user in users:
        keys = (user, account_id)
        ledgers = {m: _ledger_with_position(conn, keys, m) for m in group}
        corrected = {m: _corrected_under(conn, keys, m) for m in group}
        _snapshot_group_histories(conn, keys, group, ledgers, corrected)

        decisions = {
            row["merchant"]: row for row in conn.execute(
                "SELECT merchant, decision, decided_at FROM "
                "subscription_decisions WHERE user_id = ? AND account_id = ? "
                f"AND merchant IN ({marks})", (*keys, *group))
        }
        if any(m in decisions for m in aliases):
            winner = max(decisions.values(), key=lambda r: (
                r["decided_at"], r["decision"] == "dismissed"))
            conn.execute(
                "DELETE FROM subscription_decisions WHERE user_id = ? "
                f"AND account_id = ? AND merchant IN ({marks})", (*keys, *group))
            conn.execute(
                "INSERT INTO subscription_decisions (user_id, account_id, "
                "merchant, decision, decided_at) VALUES (?, ?, ?, ?, ?)",
                (*keys, new, winner["decision"], winner["decided_at"]))

        ledger = _merge_ledgers(ledgers)
        if ledger:
            conn.execute(
                "DELETE FROM subscription_price_observed WHERE user_id = ? "
                f"AND account_id = ? AND merchant IN ({marks})", (*keys, *group))
            conn.executemany(
                "INSERT INTO subscription_price_observed (user_id, account_id, "
                "merchant, charge_id, seq) VALUES (?, ?, ?, ?, ?)",
                [(*keys, new, charge, seq)
                 for seq, charge in enumerate(ledger, start=1)])

        conn.execute(
            "INSERT INTO subscription_price_watch (user_id, account_id, "
            "merchant, scanned_through) SELECT user_id, account_id, ?, "
            "max(scanned_through) FROM subscription_price_watch WHERE "
            f"user_id = ? AND account_id = ? AND merchant IN ({marks}) "
            "HAVING count(*) > 0 "
            "ON CONFLICT(user_id, account_id, merchant) DO UPDATE SET "
            "scanned_through = excluded.scanned_through",
            (new, *keys, *group))
        conn.execute(
            "DELETE FROM subscription_price_watch WHERE user_id = ? AND "
            f"account_id = ? AND merchant IN ({marks}) AND merchant <> ?",
            (*keys, *group, new))

        for table in _MERCHANT_KEYED:
            columns = _table_columns(conn, table)
            selected = ", ".join("?" if c == "merchant" else c for c in columns)
            for old in sorted(aliases):
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) "
                    f"SELECT {selected} FROM {table} WHERE user_id = ? "
                    "AND account_id = ? AND merchant = ?", (new, *keys, old))
                conn.execute(
                    f"DELETE FROM {table} WHERE user_id = ? AND account_id = ? "
                    "AND merchant = ?", (*keys, old))


def to_v12(conn: sqlite3.Connection) -> None:
    if "history" not in _table_columns(conn, "subscription_price_increases"):
        conn.execute(
            "ALTER TABLE subscription_price_increases ADD COLUMN history TEXT")
    if "balance_known" not in _table_columns(conn, "accounts"):
        conn.execute(
            "ALTER TABLE accounts ADD COLUMN balance_known INTEGER NOT NULL "
            "DEFAULT 1 CHECK (balance_known IN (0, 1))")
    conn.execute(
        "UPDATE accounts SET balance_known = 0 WHERE provider = 'csv'")

    candidates = {}
    for row in conn.execute(
        "SELECT id, account_id, name, merchant, provider, raw FROM transactions"
    ).fetchall():
        if not row["name"].strip().upper().startswith(_LEGACY_NETFLIX_PREFIX):
            continue
        if _provider_named(row["provider"], row["raw"]):
            continue
        old = _legacy_fallback(row["name"])
        if old == _NETFLIX_MERCHANT:
            continue
        ids = candidates.setdefault((row["account_id"], old), [])
        if row["merchant"] == old:
            ids.append(row["id"])
    for (account_id, old), ids in candidates.items():
        conn.executemany(
            "UPDATE transactions SET merchant = ? WHERE id = ?",
            [(_NETFLIX_MERCHANT, i) for i in ids])
    groups = {}
    for account_id, old in candidates:
        if conn.execute(
            "SELECT 1 FROM transactions WHERE account_id = ? AND merchant = ?",
            (account_id, old)
        ).fetchone() is None:
            groups.setdefault(account_id, []).append(old)
    for account_id in sorted(groups):
        _rekey_group(conn, account_id, groups[account_id], _NETFLIX_MERCHANT)


def to_v13(conn: sqlite3.Connection) -> None:
    if "history" not in _table_columns(conn, "subscription_price_increases"):
        conn.execute(
            "ALTER TABLE subscription_price_increases ADD COLUMN history TEXT")


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    6: to_v7,
    7: to_v8,
    8: to_v9,
    9: to_v10,
    10: to_v11,
    11: to_v12,
    12: to_v13,
}
