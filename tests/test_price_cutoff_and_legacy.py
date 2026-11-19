from __future__ import annotations

import sqlite3

import pytest

from core import repository, store, users
from tests.test_price_increases import (
    EXAMPLE, FLAT3, MERCHANT, U, account_id, d, env, increases, load, query,
    render, section, track,
)
from tests.test_price_reconcile import save_changes, stored_events, txn_id

LEGACY_WATCH = """
CREATE TABLE subscription_price_watch (
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id      TEXT NOT NULL REFERENCES accounts(id),
    merchant        TEXT NOT NULL,
    scanned_through TEXT NOT NULL,

    PRIMARY KEY (user_id, account_id, merchant),
    CHECK (length(merchant) > 0),
    CHECK (length(scanned_through) = 10)
)
"""

LEGACY_EVENTS_FIRST = """
CREATE TABLE subscription_price_events (
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id      TEXT NOT NULL REFERENCES accounts(id),
    merchant        TEXT NOT NULL,
    charge_id       TEXT NOT NULL,
    charge_date     TEXT NOT NULL,
    old_cents       INTEGER NOT NULL,
    new_cents       INTEGER NOT NULL,

    PRIMARY KEY (user_id, account_id, merchant, charge_id),
    CHECK (length(charge_date) = 10),
    CHECK (new_cents > old_cents)
)
"""

OBSERVED_SECOND = """
CREATE TABLE subscription_price_observed (
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id      TEXT NOT NULL REFERENCES accounts(id),
    merchant        TEXT NOT NULL,
    charge_id       TEXT NOT NULL,
    seq             INTEGER NOT NULL,

    PRIMARY KEY (user_id, account_id, merchant, charge_id),
    CHECK (length(merchant) > 0)
)
"""

EVENTS_SECOND = """
CREATE TABLE subscription_price_events (
    user_id            TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id         TEXT NOT NULL REFERENCES accounts(id),
    merchant           TEXT NOT NULL,
    charge_id          TEXT NOT NULL,
    previous_charge_id TEXT NOT NULL,
    charge_date        TEXT NOT NULL,
    old_cents          INTEGER NOT NULL,
    new_cents          INTEGER NOT NULL,

    PRIMARY KEY (user_id, account_id, merchant, charge_id),
    CHECK (length(charge_date) = 10),
    CHECK (new_cents > old_cents)
)
"""


def increase_rows(path):
    return query(path, "SELECT charge_id, previous_charge_id, charge_date, old_cents, "
                       "new_cents FROM subscription_price_increases ORDER BY charge_date")


def test_removing_the_newest_charge_does_not_make_older_history_look_new(env):
    _, path = env
    load(env, "a.csv", FLAT3)
    track(path)
    october = txn_id(path, "2026-10-12", 1599)
    save_changes(path, removed=[october])
    load(env, "b.csv", [(d(9, 30), "17.99")])
    assert query(path, "SELECT count(*) FROM transactions")[0][0] == 3
    assert increases(path) == []
    assert query(path, "SELECT scanned_through FROM subscription_price_watch")[0][0] == "2026-10-12"

    load(env, "c.csv", [(d(11), "19.99")])
    (w,) = increases(path)
    assert (w.date, w.old_cents, w.new_cents) == (
        __import__("datetime").date(2026, 11, 12), 1799, 1999)


def test_the_cutoff_survives_removing_every_eligible_charge(env):
    _, path = env
    load(env, "a.csv", FLAT3)
    track(path)
    ids = [r[0] for r in query(path, "SELECT id FROM transactions")]
    save_changes(path, removed=ids)
    assert query(path, "SELECT count(*) FROM transactions")[0][0] == 0
    load(env, "b.csv", [(d(9, 30), "17.99")])
    assert increases(path) == []
    load(env, "c.csv", [(d(10, 20), "15.99"), (d(11), "17.99")])
    (w,) = increases(path)
    assert (w.old_cents, w.new_cents) == (1599, 1799)


def test_a_correction_moving_the_latest_date_backward_keeps_the_cutoff(env):
    _, path = env
    load(env, "a.csv", FLAT3)
    track(path)
    october = txn_id(path, "2026-10-12", 1599)
    from datetime import date
    save_changes(path, modified=[(october, {"date": date(2026, 9, 20)})])
    load(env, "b.csv", [(d(10, 1), "17.99")])
    assert increases(path) == []
    load(env, "c.csv", [(d(10, 20), "19.99")])
    assert len(increases(path)) == 1


def test_a_same_day_charge_and_repeat_import_still_work_after_a_removal(env):
    _, path = env
    load(env, "a.csv", FLAT3)
    track(path)
    load(env, "b.csv", [(d(10), "17.99")])
    (event,) = stored_events(path)
    load(env, "b.csv", [(d(10), "17.99")])
    assert stored_events(path) == [event]


def pair_history(env, rows):
    _, path = env
    load(env, "a.csv", rows)
    track(path)
    return path, stored_events(path)


def test_a_date_correction_that_leaves_a_flat_pair_clears_the_warning(env):
    path, events = pair_history(env, [(d(8), "17.99"), (d(9), "15.99"), (d(10), "17.99")])
    assert len(events) == 1
    from datetime import date
    save_changes(path, modified=[(events[0][3], {"date": date(2026, 11, 12)})])
    assert stored_events(path) == []
    assert "Price Increases" not in render(path)


def test_a_date_correction_with_a_different_valid_predecessor_updates_the_event(env):
    path, events = pair_history(env, [(d(8), "14.99"), (d(9), "15.99"), (d(10), "17.99")])
    (event,) = events
    september = event[3]
    august = txn_id(path, "2026-08-12", 1499)
    from datetime import date
    save_changes(path, modified=[(september, {"date": date(2026, 11, 12)})])
    (moved,) = stored_events(path)
    assert moved[2] == event[2] and moved[3] == august
    assert moved[4:] == ("2026-10-12", 1499, 1799)
    (w,) = increases(path)
    assert (w.old_cents, w.new_cents, w.delta_cents) == (1499, 1799, 300)
    assert "+$3.00" in section(path)


def test_a_charge_moved_between_the_pair_becomes_the_predecessor(env):
    path, events = pair_history(env, [(d(8), "14.99"), (d(9), "15.99"), (d(10), "17.99")])
    august = txn_id(path, "2026-08-12", 1499)
    from datetime import date
    save_changes(path, modified=[(august, {"date": date(2026, 9, 20)})])
    (moved,) = stored_events(path)
    assert moved[3] == august and moved[5:] == (1499, 1799)


def test_a_later_decrease_and_an_older_backfill_leave_the_pair_alone(env):
    path, events = pair_history(env, EXAMPLE)
    load(env, "b.csv", [(d(11), "15.99")])
    load(env, "c.csv", [(d(7), "13.99"), (d(9, 20), "9.99")])
    assert stored_events(path) == events
    assert query(path, "SELECT count(*) FROM transactions")[0][0] == 6


def legacy_example(env, shape, reconstructable=True):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    (event,) = stored_events(path)
    acct = account_id(path)
    con = sqlite3.connect(str(path))
    for table in ("subscription_price_observed", "subscription_price_watch",
                  "subscription_price_increases"):
        con.execute(f"DROP TABLE {table}")
    old_cents = 1599 if reconstructable else 1499
    if shape == "first":
        con.execute(LEGACY_WATCH)
        con.execute(LEGACY_EVENTS_FIRST)
        con.execute("INSERT INTO subscription_price_watch VALUES (?, ?, ?, ?)",
                    (U, acct, MERCHANT, "2026-10-12"))
        con.execute("INSERT INTO subscription_price_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (U, acct, MERCHANT, event[2], "2026-10-12", old_cents, 1799))
    else:
        con.execute(OBSERVED_SECOND)
        con.execute(EVENTS_SECOND)
        for seq, (txn,) in enumerate(con.execute(
                "SELECT id FROM transactions ORDER BY date").fetchall(), start=1):
            pass
        rows = con.execute("SELECT id FROM transactions ORDER BY date").fetchall()
        for seq, (txn,) in enumerate(rows, start=1):
            con.execute("INSERT INTO subscription_price_observed VALUES (?, ?, ?, ?, ?)",
                        (U, acct, MERCHANT, txn, seq))
        con.execute("INSERT INTO subscription_price_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (U, acct, MERCHANT, event[2], event[3], "2026-10-12", 1599, 1799))
    con.execute("UPDATE meta SET schema_version = 9")
    con.commit()
    con.close()
    return path, event


@pytest.mark.parametrize("shape", ["first", "second"])
def test_an_earlier_v9_database_upgrades_renders_and_accepts_a_later_import(env, shape):
    path, event = legacy_example(env, shape)
    ids = query(path, "SELECT id FROM transactions ORDER BY id")
    decisions = query(path, "SELECT account_id, merchant, decision FROM subscription_decisions")
    assert len(ids) == 3 and len(decisions) == 1

    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == [10, 11]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    assert query(path, "SELECT id FROM transactions ORDER BY id") == ids
    assert query(path, "SELECT account_id, merchant, decision FROM subscription_decisions") == decisions
    assert query(path, "SELECT schema_version FROM meta")[0][0] == store.SCHEMA_VERSION
    assert increase_rows(path) == [(event[2], event[3], "2026-10-12", 1599, 1799)]
    assert "+$2.00" in section(path) and "2026-10-12" in section(path)

    load(env, "b.csv", [(d(11), "17.99")])
    assert increase_rows(path) == [(event[2], event[3], "2026-10-12", 1599, 1799)]
    load(env, "c.csv", [(d(12), "19.99")])
    assert [(r[2], r[3], r[4]) for r in increase_rows(path)] == [
        ("2026-10-12", 1599, 1799), ("2026-12-12", 1799, 1999)]
    load(env, "d.csv", [(d(7), "13.99")])
    assert len(increase_rows(path)) == 2


def test_an_unreconstructable_earlier_v9_warning_is_dropped_not_guessed(env):
    path, _ = legacy_example(env, "first", reconstructable=False)
    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == [10, 11]
    finally:
        conn.close()
    assert increase_rows(path) == []
    assert query(path, "SELECT count(*) FROM subscription_price_observed")[0][0] == 3
    load(env, "b.csv", [(d(11), "19.99")])
    assert len(increase_rows(path)) == 1
