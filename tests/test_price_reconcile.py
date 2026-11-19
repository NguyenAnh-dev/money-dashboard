from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from core import repository, store, users
from core.models import TransactionKind
from tests.test_price_increases import (
    ACCT, EXAMPLE, FLAT3, MERCHANT, U, account_id, d, env, increases, load,
    query, render, section, track,
)


def stored_events(path):
    return query(path, "SELECT account_id, merchant, charge_id, previous_charge_id, "
                       "charge_date, old_cents, new_cents FROM subscription_price_increases "
                       "ORDER BY charge_date, charge_id")


def txn_id(path, day, cents, merchant=MERCHANT):
    rows = query(path, f"SELECT id FROM transactions WHERE date = '{day}' "
                       f"AND amount_cents = {cents} AND merchant = '{merchant}'")
    assert len(rows) == 1
    return rows[0][0]


def save_changes(path, modified=(), removed=()):
    conn = store.open_db(str(path))
    try:
        by_id = {t.id: t for t in repository.load_transactions(conn, U)}
        changed = [dataclasses.replace(by_id[i], **fields) for i, fields in modified]
        repository.save(conn, U, [], changed, removed=list(removed))
    finally:
        conn.close()


def tracked_example(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    events = stored_events(path)
    assert len(events) == 1
    return path, events[0]


def test_correcting_the_introducing_charge_back_clears_the_warning(env):
    path, event = tracked_example(env)
    save_changes(path, modified=[(event[2], {"amount_cents": 1599})])
    assert increases(path) == [] and stored_events(path) == []
    assert "Price Increases" not in render(path)


def test_removing_the_introducing_charge_clears_the_warning(env):
    path, event = tracked_example(env)
    save_changes(path, removed=[event[2]])
    assert increases(path) == []
    assert stored_events(path) == []


def test_a_still_valid_correction_updates_amounts_without_a_second_event(env):
    path, event = tracked_example(env)
    save_changes(path, modified=[(event[2], {"amount_cents": 1899})])
    (only,) = stored_events(path)
    assert only[2] == event[2] and only[5:] == (1599, 1899)
    (w,) = increases(path)
    assert (w.old_cents, w.new_cents, w.delta_cents) == (1599, 1899, 300)
    assert "+$3.00" in section(path)


def test_predecessor_corrections_update_or_clear(env):
    path, event = tracked_example(env)
    prev = event[3]
    save_changes(path, modified=[(prev, {"amount_cents": 1699})])
    (w,) = increases(path)
    assert (w.old_cents, w.new_cents, w.delta_cents) == (1699, 1799, 100)
    save_changes(path, modified=[(prev, {"amount_cents": 1799})])
    assert increases(path) == []


CHANGES = [
    {"amount_cents": -1599},
    {"merchant": "Other"},
    {"pending": True},
    {"kind": TransactionKind.TRANSFER},
]


@pytest.mark.parametrize("change", CHANGES)
def test_an_introducing_charge_made_ineligible_or_moved_clears_the_warning(env, change):
    path, event = tracked_example(env)
    save_changes(path, modified=[(event[2], change)])
    assert increases(path) == []
    assert stored_events(path) == []


@pytest.mark.parametrize("change", CHANGES)
def test_a_predecessor_made_ineligible_falls_back_to_the_next_earlier_charge(env, change):
    path, event = tracked_example(env)
    august = txn_id(path, "2026-08-12", 1599)
    save_changes(path, modified=[(event[3], change)])
    (moved,) = stored_events(path)
    assert moved[2] == event[2] and moved[3] == august
    assert moved[4:] == ("2026-10-12", 1599, 1799)


@pytest.mark.parametrize("change", CHANGES)
def test_a_predecessor_made_ineligible_clears_when_no_rise_remains(env, change):
    _, path = env
    load(env, "a.csv", [(d(8), "17.99"), (d(9), "15.99"), (d(10), "17.99")])
    track(path)
    (event,) = stored_events(path)
    save_changes(path, modified=[(event[3], change)])
    assert stored_events(path) == []


def test_a_later_decrease_keeps_the_earlier_warning_after_reconcile(env):
    path, event = tracked_example(env)
    load(env, "b.csv", [(d(11), "15.99")])
    assert stored_events(path) == [event]
    assert len(increases(path)) == 1


def test_reconcile_leaves_the_other_card_alone(env):
    _, path = env
    load(env, "a.csv", EXAMPLE, account="card-a")
    load(env, "b.csv", EXAMPLE, account="card-b")
    ids = {n: i for i, n in query(path, "SELECT id, name FROM accounts")}
    track(path, account=ids["card-a"])
    track(path, account=ids["card-b"])
    assert len(stored_events(path)) == 2
    a_charge = [e for e in stored_events(path) if e[0] == ids["card-a"]][0][2]
    save_changes(path, modified=[(a_charge, {"amount_cents": 1599})])
    assert [e[0] for e in stored_events(path)] == [ids["card-b"]]


def test_an_omitted_row_is_not_a_deletion(env):
    path, event = tracked_example(env)
    load(env, "b.csv", [(d(8), "15.99")])
    assert stored_events(path) == [event]


def test_a_new_same_day_charge_after_a_flat_one_warns_once_and_is_stable(env):
    _, path = env
    load(env, "a.csv", FLAT3)
    track(path)
    flat = [(d(11), "15.99")]
    bump = [(d(11), "17.99")]
    load(env, "b.csv", flat)
    assert stored_events(path) == []
    load(env, "c.csv", bump)
    (event,) = stored_events(path)
    assert event[2] == txn_id(path, "2026-11-12", 1799)
    assert event[3] == txn_id(path, "2026-11-12", 1599)
    assert event[4:] == ("2026-11-12", 1599, 1799)

    load(env, "b.csv", flat)
    load(env, "c.csv", bump)
    assert stored_events(path) == [event]
    assert len(increases(path)) == 1 and "+$2.00" in section(path)


def test_same_day_order_within_one_import_is_statement_order(env):
    _, path = env
    load(env, "a.csv", FLAT3)
    track(path)
    load(env, "b.csv", [(d(11), "15.99"), (d(11), "17.99")])
    assert len(stored_events(path)) == 1
    load(env, "c.csv", [(d(12), "17.99"), (d(12), "15.99")])
    assert len(stored_events(path)) == 1


def test_older_backfill_on_an_earlier_day_still_never_warns(env):
    _, path = env
    load(env, "a.csv", FLAT3)
    track(path)
    load(env, "b.csv", [(d(7), "13.99"), (d(8, 5), "19.99"), (d(8), "15.99")])
    assert query(path, "SELECT count(*) FROM transactions")[0][0] == 5
    assert stored_events(path) == []


def _make_v8(path):
    con = sqlite3.connect(str(path))
    con.execute("DROP TABLE subscription_price_observed")
    con.execute("DROP TABLE subscription_price_watch")
    con.execute("DROP TABLE subscription_price_increases")
    con.execute("UPDATE meta SET schema_version = 8")
    con.commit()
    con.close()


def test_a_tracked_v8_subscription_keeps_watching_after_upgrade(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    load(env, "h.csv", EXAMPLE, description="HULU")
    acct = account_id(path)
    conn = store.open_db(str(path))
    try:
        repository.set_decision(conn, U, acct, MERCHANT, repository.TRACKED)
        repository.set_decision(conn, U, acct, "Hulu", repository.DISMISSED)
    finally:
        conn.close()
    ids = query(path, "SELECT id FROM transactions ORDER BY id")
    decisions = query(path, "SELECT merchant, decision FROM subscription_decisions ORDER BY merchant")
    assert len(decisions) == 2
    _make_v8(path)

    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == [9, 10, 11]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    assert query(path, "SELECT id FROM transactions ORDER BY id") == ids
    assert query(path, "SELECT merchant, decision FROM subscription_decisions ORDER BY merchant") == decisions
    assert stored_events(path) == []
    observed = query(path, "SELECT merchant, count(*) FROM subscription_price_observed GROUP BY merchant")
    assert observed == [(MERCHANT, 3)]

    load(env, "n.csv", [(d(11), "19.99")])
    (event,) = stored_events(path)
    assert event[1] == MERCHANT
    assert event[2] == txn_id(path, "2026-11-12", 1999)
    assert event[3] == txn_id(path, "2026-10-12", 1799)
    assert event[4:] == ("2026-11-12", 1799, 1999)
    assert [w.merchant for w in increases(path)] == [MERCHANT]


def test_a_tracked_v8_subscription_with_no_history_starts_when_history_arrives(env):
    _, path = env
    load(env, "x.csv", [(d(1), "5.00")], description="OTHER")
    track(path, merchant=MERCHANT)
    _make_v8(path)
    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == [9, 10, 11]
    finally:
        conn.close()
    assert query(path, "SELECT count(*) FROM subscription_price_observed")[0][0] == 0
    load(env, "a.csv", EXAMPLE)
    assert len(stored_events(path)) == 1


@pytest.mark.parametrize("version", [8, 7, 6])
def test_upgrade_chains_initialise_tracked_subscriptions(tmp_path, version):
    from adapters.fake import FakeSource
    path = tmp_path / f"v{version}.db"
    conn = store.open_db(str(path))
    try:
        user = users.ensure_local_user(conn)
        source = FakeSource()
        repository.save(conn, user, source.fetch_accounts(),
                        source.fetch_transactions().added,
                        connection=source.connection())
        account, merchant = conn.execute(
            "SELECT account_id, merchant FROM transactions WHERE amount_cents > 0 "
            "AND pending = 0 AND kind <> 'transfer' GROUP BY account_id, merchant "
            "ORDER BY count(*) DESC LIMIT 1").fetchone()
        repository.set_decision(conn, user, account, merchant, repository.TRACKED)
    finally:
        conn.close()
    expected = query(path, "SELECT count(*) FROM transactions WHERE account_id = '%s' "
                           "AND merchant = '%s' AND amount_cents > 0 AND pending = 0 "
                           "AND kind <> 'transfer'" % (account, merchant))[0][0]
    ids = query(path, "SELECT id FROM transactions ORDER BY id")
    provider_ids = query(path, "SELECT provider_id FROM transactions ORDER BY provider_id")
    assert expected >= 1 and ids
    _make_v8(path)
    con = sqlite3.connect(str(path))
    if version == 6:
        con.execute("DELETE FROM subscription_decisions")
        expected = 0
    con.execute("UPDATE meta SET schema_version = ?", (version,))
    con.commit()
    con.close()

    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == list(range(version + 1, 12))
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    if version == 6:
        assert query(path, "SELECT provider_id FROM transactions ORDER BY provider_id") == provider_ids
    else:
        assert query(path, "SELECT id FROM transactions ORDER BY id") == ids
    assert query(path, "SELECT count(*) FROM subscription_price_observed")[0][0] == expected
    assert query(path, "SELECT schema_version FROM meta")[0][0] == store.SCHEMA_VERSION
