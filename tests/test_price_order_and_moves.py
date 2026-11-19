from __future__ import annotations

import dataclasses
from datetime import date

from core import repository, store
from tests.test_price_increases import (
    EXAMPLE, FLAT3, MERCHANT, U, d, env, increases, load, query, section, track,
)
from tests.test_price_reconcile import save_changes, stored_events, txn_id


def seqs(path):
    return dict(query(path, "SELECT charge_id, seq FROM subscription_price_observed"))


def test_temporary_ineligibility_keeps_the_first_observed_order(env):
    _, path = env
    load(env, "a.csv", FLAT3)
    track(path)
    load(env, "b.csv", [(d(11), "15.99"), (d(11), "13.99")])
    assert increases(path) == []
    first = txn_id(path, "2026-11-12", 1599)
    second = txn_id(path, "2026-11-12", 1399)
    before = seqs(path)
    assert before[first] < before[second]

    save_changes(path, modified=[(first, {"pending": True})])
    assert increases(path) == [] and stored_events(path) == []
    assert first in seqs(path)

    save_changes(path, modified=[(first, {"pending": False})])
    assert increases(path) == [] and stored_events(path) == []
    assert seqs(path) == before
    assert query(path, "SELECT count(*) FROM transactions")[0][0] == 5


def test_a_charge_first_seen_pending_is_evaluated_when_it_first_posts(env):
    _, path = env
    load(env, "a.csv", FLAT3)
    track(path)
    conn = store.open_db(str(path))
    try:
        base = repository.load_transactions(conn, U)[0]
        source = dataclasses.replace(base.source, provider_id="pending-first")
        pending = dataclasses.replace(
            base, id="csv:pending-first", source=source, date=date(2026, 11, 12),
            amount_cents=1799, pending=True)
        repository.save(conn, U, [], [pending])
    finally:
        conn.close()
    assert increases(path) == []
    assert "csv:pending-first" not in seqs(path)

    save_changes(path, modified=[("csv:pending-first", {"pending": False})])
    (w,) = increases(path)
    assert w.charge_id == "csv:pending-first"
    assert (w.date, w.old_cents, w.new_cents) == (date(2026, 11, 12), 1599, 1799)


def test_a_genuinely_new_same_day_charge_still_warns_after_a_pending_round_trip(env):
    _, path = env
    load(env, "a.csv", FLAT3)
    track(path)
    load(env, "b.csv", [(d(11), "15.99")])
    first = txn_id(path, "2026-11-12", 1599)
    save_changes(path, modified=[(first, {"pending": True})])
    save_changes(path, modified=[(first, {"pending": False})])
    load(env, "c.csv", [(d(11), "17.99")])
    (event,) = stored_events(path)
    assert event[2] == txn_id(path, "2026-11-12", 1799) and event[3] == first


def with_later_charge(env, cents):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    (event,) = stored_events(path)
    load(env, "b.csv", [(d(11), cents)])
    assert stored_events(path) == [event]
    return path, event


def test_correcting_a_later_charge_into_the_pair_clears_a_flat_warning(env):
    path, event = with_later_charge(env, "17.99")
    november = txn_id(path, "2026-11-12", 1799)
    save_changes(path, modified=[(november, {"date": date(2026, 9, 30)})])
    assert stored_events(path) == []
    assert increases(path) == []


def test_correcting_a_later_charge_into_the_pair_updates_a_valid_rise(env):
    path, event = with_later_charge(env, "16.99")
    november = txn_id(path, "2026-11-12", 1699)
    save_changes(path, modified=[(november, {"date": date(2026, 9, 30)})])
    (moved,) = stored_events(path)
    assert moved[2] == event[2] and moved[3] == november
    assert moved[4:] == ("2026-10-12", 1699, 1799)
    (w,) = increases(path)
    assert (w.old_cents, w.new_cents, w.delta_cents) == (1699, 1799, 100)
    assert "+$1.00" in section(path)

    load(env, "c.csv", [(d(12), "17.99")])
    assert stored_events(path) == [moved]


def test_an_unknown_older_backfill_still_leaves_the_warning_alone(env):
    path, event = with_later_charge(env, "17.99")
    load(env, "c.csv", [(d(9, 30), "17.99")])
    assert query(path, "SELECT count(*) FROM transactions")[0][0] == 5
    assert stored_events(path) == [event]


def test_re_saving_a_later_charge_with_an_unchanged_date_changes_nothing(env):
    path, event = with_later_charge(env, "17.99")
    november = txn_id(path, "2026-11-12", 1799)
    save_changes(path, modified=[(november, {"amount_cents": 1799})])
    assert stored_events(path) == [event]


def event_state(path):
    (event,) = stored_events(path)
    return event


def test_a_corrected_charge_stays_in_the_comparison_through_a_pending_round_trip(env):
    path, october = with_later_charge(env, "16.99")
    november = txn_id(path, "2026-11-12", 1699)
    september = txn_id(path, "2026-09-12", 1599)
    intro = october[2]

    save_changes(path, modified=[(november, {"date": date(2026, 9, 30)})])
    assert event_state(path) == (october[0], october[1], intro, november,
                                 "2026-10-12", 1699, 1799)

    save_changes(path, modified=[(november, {"pending": True})])
    assert event_state(path) == (october[0], october[1], intro, september,
                                 "2026-10-12", 1599, 1799)
    assert [w.delta_cents for w in increases(path)] == [200]

    load(env, "c.csv", [(d(12), "17.99")])
    save_changes(path, modified=[(november, {"pending": False})])
    assert event_state(path) == (october[0], october[1], intro, november,
                                 "2026-10-12", 1699, 1799)
    assert [w.delta_cents for w in increases(path)] == [100]
    assert query(path, "SELECT count(*) FROM subscription_price_corrected")[0][0] == 1

    load(env, "c.csv", [(d(12), "17.99")])
    save_changes(path, modified=[(november, {"amount_cents": 1699})])
    assert len(stored_events(path)) == 1
    assert event_state(path)[3:] == (november, "2026-10-12", 1699, 1799)


def test_an_older_backfill_with_the_corrected_charges_date_and_amount_stays_excluded(env):
    path, october = with_later_charge(env, "16.99")
    november = txn_id(path, "2026-11-12", 1699)
    save_changes(path, modified=[(november, {"date": date(2026, 9, 30)})])
    before = event_state(path)
    assert before[3] == november

    load(env, "d.csv", [(d(9, 30), "16.99")])
    assert query(path, "SELECT count(*) FROM transactions WHERE date = '2026-09-30'")[0][0] == 2
    assert event_state(path) == before
    save_changes(path, modified=[(november, {"pending": True})])
    save_changes(path, modified=[(november, {"pending": False})])
    assert event_state(path) == before


def test_a_v10_file_without_the_corrected_table_opens_and_gains_it(env):
    import sqlite3
    path, _ = with_later_charge(env, "16.99")
    con = sqlite3.connect(str(path))
    con.execute("DROP TABLE subscription_price_corrected")
    con.execute("UPDATE meta SET schema_version = 10")
    con.commit()
    con.close()
    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == [11]
    finally:
        conn.close()
    assert query(path, "SELECT count(*) FROM subscription_price_corrected")[0][0] == 0
    november = txn_id(path, "2026-11-12", 1699)
    save_changes(path, modified=[(november, {"date": date(2026, 9, 30)})])
    assert event_state(path)[3] == november


def earlier_v10(path):
    import sqlite3
    con = sqlite3.connect(str(path))
    con.execute("DROP TABLE subscription_price_corrected")
    con.execute("UPDATE meta SET schema_version = 10")
    con.commit()
    con.close()


def upgrade(path):
    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == [11]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_an_earlier_v10_corrected_predecessor_is_recovered_on_upgrade(env):
    path, october = with_later_charge(env, "16.99")
    november = txn_id(path, "2026-11-12", 1699)
    september = txn_id(path, "2026-09-12", 1599)
    intro = october[2]
    save_changes(path, modified=[(november, {"date": date(2026, 9, 30)})])
    corrected = (october[0], october[1], intro, november, "2026-10-12", 1699, 1799)
    assert event_state(path) == corrected
    observed = seqs(path)
    assert observed[november] > observed[intro]
    ids = query(path, "SELECT id FROM transactions ORDER BY id")
    decisions = query(path, "SELECT * FROM subscription_decisions")
    watch = query(path, "SELECT * FROM subscription_price_watch")
    earlier_v10(path)
    assert not query(path, "SELECT name FROM sqlite_master WHERE name = 'subscription_price_corrected'")

    upgrade(path)
    assert query(path, "SELECT charge_id FROM subscription_price_corrected") == [(november,)]
    assert query(path, "SELECT id FROM transactions ORDER BY id") == ids
    assert query(path, "SELECT * FROM subscription_decisions") == decisions
    assert query(path, "SELECT * FROM subscription_price_watch") == watch
    assert seqs(path) == observed
    assert event_state(path) == corrected

    save_changes(path, modified=[(november, {"pending": True})])
    assert event_state(path) == (october[0], october[1], intro, september,
                                 "2026-10-12", 1599, 1799)
    assert [w.delta_cents for w in increases(path)] == [200]

    save_changes(path, modified=[(november, {"pending": False})])
    assert event_state(path) == corrected
    assert [w.delta_cents for w in increases(path)] == [100]
    assert len(stored_events(path)) == 1

    load(env, "c.csv", [(d(12), "17.99")])
    assert event_state(path) == corrected
    assert "+$1.00" in section(path)


def test_the_recovery_is_safe_to_run_again(env):
    path, october = with_later_charge(env, "16.99")
    november = txn_id(path, "2026-11-12", 1699)
    save_changes(path, modified=[(november, {"date": date(2026, 9, 30)})])
    earlier_v10(path)
    upgrade(path)
    from core import migrations
    conn = store.connect(str(path))
    try:
        with conn:
            migrations.to_v11(conn)
            migrations.to_v11(conn)
    finally:
        conn.close()
    assert query(path, "SELECT charge_id FROM subscription_price_corrected") == [(november,)]


def test_upgrade_does_not_promote_an_ordinary_older_backfill(env):
    path, october = with_later_charge(env, "17.99")
    load(env, "d.csv", [(d(9, 30), "17.99")])
    before = event_state(path)
    assert before == october
    backfill = txn_id(path, "2026-09-30", 1799)
    assert seqs(path)[backfill] > seqs(path)[october[2]]
    earlier_v10(path)
    upgrade(path)
    assert query(path, "SELECT count(*) FROM subscription_price_corrected")[0][0] == 0
    assert event_state(path) == before
    save_changes(path, modified=[(backfill, {"pending": True})])
    save_changes(path, modified=[(backfill, {"pending": False})])
    assert event_state(path) == before
