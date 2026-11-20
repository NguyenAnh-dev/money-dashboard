from __future__ import annotations

import sqlite3
from datetime import date, datetime
from io import StringIO

import pytest
from rich.console import Console

import main as app
from adapters import merchant as merchant_module
from core import repository, store, users
from tests.test_price_increases import (
    EXAMPLE, U, account_id, d, env, increases, load, query, render, track,
)

LEGACY = "Netflix.Com"


def raw(path, sql, args=()):
    con = sqlite3.connect(str(path))
    try:
        con.execute(sql, args)
        con.commit()
    finally:
        con.close()


def legacy_db(env, rows=EXAMPLE):
    _, path = env
    with pytest.MonkeyPatch.context() as patch:
        patch.delitem(merchant_module.MERCHANT_PREFIXES, "NETFLIX")
        load(env, "legacy.csv", rows)
    assert {m for (m,) in query(path, "SELECT merchant FROM transactions")} == {LEGACY}
    return path


def downgrade(path):
    raw(path, "UPDATE meta SET schema_version = 11")


def migrate(path):
    downgrade(path)
    store.open_db(str(path)).close()


def decide(path, merchant, decision, stamp):
    conn = store.open_db(str(path))
    try:
        repository.set_decision(conn, U, account_id(path), merchant, decision,
                                decided_at=datetime.fromisoformat(stamp))
    finally:
        conn.close()


def acknowledge_open(path, merchant):
    conn = store.open_db(str(path))
    try:
        [w] = repository.load_price_increases(conn, U)
        assert repository.acknowledge_increase(conn, U, w.account_id, merchant,
                                               w.charge_id)
        return w
    finally:
        conn.close()


def keys(path, table):
    return {m for (m,) in query(path, f"SELECT merchant FROM {table}")}


def recurring_section(path):
    return render(path).split("Recurring Charges")[1].split("Accounts")[0]


def test_legacy_tracked_state_and_open_warning_survive_a_full_reimport(env):
    _, path = env
    legacy_db(env)
    track(path, LEGACY)
    [before] = increases(path)
    assert before.merchant == LEGACY
    downgrade(path)

    load(env, "again.csv", EXAMPLE)

    [after] = increases(path)
    assert (after.merchant, after.charge_id, after.date,
            after.old_cents, after.new_cents) == (
        "Netflix", before.charge_id, before.date, 1599, 1799)
    for table in ("subscription_decisions", "subscription_price_observed",
                  "subscription_price_increases"):
        assert keys(path, table) == {"Netflix"}
    assert keys(path, "transactions") == {"Netflix"}
    assert query(path, "SELECT count(*) FROM transactions")[0][0] == 3
    out = render(path)
    assert "+$2.00" in out and "(tracked)" in out


def test_ids_and_raw_descriptors_are_untouched_by_the_migration(env):
    _, path = env
    legacy_db(env)
    before = query(path, "SELECT id, name FROM transactions ORDER BY id")
    migrate(path)
    after = query(path, "SELECT id, name FROM transactions ORDER BY id")
    assert len(before) == 3 and before == after
    assert {n for _, n in after} == {"NETFLIX.COM"}
    assert keys(path, "transactions") == {"Netflix"}


def test_dismissed_candidate_stays_hidden_after_migration_and_reimport(env):
    _, path = env
    legacy_db(env)
    assert "Netflix" in recurring_section(path)
    decide(path, LEGACY, "dismissed", "2026-09-01T00:00:00")
    assert "Netflix" not in recurring_section(path)
    downgrade(path)

    load(env, "again.csv", EXAMPLE)

    assert query(path, "SELECT merchant, decision FROM subscription_decisions") == [
        ("Netflix", "dismissed")]
    assert "Netflix" not in recurring_section(path)


def test_acknowledged_increase_does_not_reappear_after_reimport_and_retrack(env):
    _, path = env
    legacy_db(env)
    track(path, LEGACY)
    first = acknowledge_open(path, LEGACY)
    assert increases(path) == []
    downgrade(path)

    load(env, "again.csv", EXAMPLE)
    track(path, "Netflix")

    assert increases(path) == []
    assert query(path, "SELECT merchant, charge_id FROM "
                       "subscription_price_acknowledged") == [
        ("Netflix", first.charge_id)]
    conn = store.open_db(str(path))
    try:
        [kept] = repository.load_price_increases(conn, U, include_acknowledged=True)
    finally:
        conn.close()
    assert kept.charge_id == first.charge_id


def test_acknowledgement_survives_and_a_later_increase_is_independent(env):
    _, path = env
    legacy_db(env)
    track(path, LEGACY)
    first = acknowledge_open(path, LEGACY)
    downgrade(path)

    load(env, "later.csv", [(d(10), "17.99"), (d(11), "19.99")])

    [second] = increases(path)
    assert second.charge_id != first.charge_id
    assert (second.old_cents, second.new_cents, second.merchant) == (
        1799, 1999, "Netflix")
    assert query(path, "SELECT merchant, charge_id FROM "
                       "subscription_price_acknowledged") == [
        ("Netflix", first.charge_id)]


def test_partial_reimport_does_not_split_old_and_new_charges(env):
    _, path = env
    legacy_db(env)
    track(path, LEGACY)
    [original] = increases(path)
    downgrade(path)

    load(env, "tail.csv", [(d(10), "17.99"), (d(11), "17.99")])

    assert keys(path, "transactions") == {"Netflix"}
    assert query(path, "SELECT count(*) FROM transactions")[0][0] == 4
    assert [w.charge_id for w in increases(path)] == [original.charge_id]
    assert keys(path, "subscription_decisions") == {"Netflix"}


def test_renewal_estimate_is_continuous_across_the_migration(env):
    _, path = env
    legacy_db(env)
    track(path, LEGACY)

    def renewals():
        buffer = StringIO()
        app.main(console=Console(file=buffer, width=200, color_system=None,
                                 force_terminal=False), today=date(2026, 11, 8))
        return buffer.getvalue().split("Upcoming Renewals")[1].split(
            "Recurring Charges")[0]

    before = renewals()
    assert "2026-11-12" in before and "$17.99" in before and LEGACY in before
    migrate(path)
    after = renewals()
    assert "2026-11-12" in after and "$17.99" in after
    assert "Netflix" in after and LEGACY not in after
    assert after.count("2026-11-12") == before.count("2026-11-12") == 1


def test_coexisting_keys_merge_and_the_newest_decision_wins(env):
    _, path = env
    legacy_db(env)
    first_id = query(path, "SELECT id FROM transactions ORDER BY date LIMIT 1")[0][0]
    raw(path, "UPDATE transactions SET merchant = 'Netflix' WHERE id <> ?",
        (first_id,))
    decide(path, LEGACY, "tracked", "2026-09-01T00:00:00")
    decide(path, "Netflix", "dismissed", "2026-10-01T00:00:00")
    raw(path, "INSERT INTO subscription_price_acknowledged VALUES (?, ?, ?, 'x', "
              "'2026-09-02T00:00:00')", (U, account_id(path), LEGACY))
    assert keys(path, "subscription_decisions") == {LEGACY, "Netflix"}

    migrate(path)

    assert query(path, "SELECT merchant, decision, decided_at FROM "
                       "subscription_decisions") == [
        ("Netflix", "dismissed", "2026-10-01T00:00:00")]
    assert keys(path, "subscription_price_acknowledged") == {"Netflix"}
    assert keys(path, "transactions") == {"Netflix"}


def test_coexisting_keys_an_older_new_decision_loses(env):
    _, path = env
    legacy_db(env)
    raw(path, "UPDATE transactions SET merchant = 'Netflix' WHERE date > '2026-08-12'")
    decide(path, LEGACY, "tracked", "2026-10-01T00:00:00")
    decide(path, "Netflix", "dismissed", "2026-09-01T00:00:00")
    migrate(path)
    assert query(path, "SELECT merchant, decision FROM subscription_decisions") == [
        ("Netflix", "tracked")]


def test_coexisting_keys_a_tie_goes_to_dismissed(env):
    _, path = env
    legacy_db(env)
    raw(path, "UPDATE transactions SET merchant = 'Netflix' WHERE date > '2026-08-12'")
    decide(path, LEGACY, "tracked", "2026-10-01T00:00:00")
    decide(path, "Netflix", "dismissed", "2026-10-01T00:00:00")
    migrate(path)
    assert query(path, "SELECT merchant, decision FROM subscription_decisions") == [
        ("Netflix", "dismissed")]


def test_observed_ledger_is_renumbered_old_first_without_duplicates(env):
    _, path = env
    legacy_db(env)
    track(path, LEGACY)
    old_order = [c for (c,) in query(path, "SELECT charge_id FROM "
                                           "subscription_price_observed ORDER BY seq")]
    assert len(old_order) == 3
    migrate(path)
    assert [c for (c,) in query(path, "SELECT charge_id FROM "
                                      "subscription_price_observed ORDER BY seq")
            ] == old_order
    assert [s for (s,) in query(path, "SELECT seq FROM subscription_price_observed "
                                      "ORDER BY seq")] == [1, 2, 3]


def test_a_key_still_shared_with_unrelated_rows_keeps_its_state(env):
    _, path = env
    legacy_db(env)
    track(path, LEGACY)
    stranger = query(path, "SELECT id FROM transactions ORDER BY date LIMIT 1")[0][0]
    raw(path, "UPDATE transactions SET name = 'Something Else' WHERE id = ?",
        (stranger,))
    migrate(path)
    assert keys(path, "subscription_decisions") == {LEGACY}
    assert query(path, "SELECT merchant FROM transactions WHERE id = '%s'"
                 % stranger) == [(LEGACY,)]


def earlier_build_reimport(env, acknowledged):
    _, path = env
    legacy_db(env)
    track(path, LEGACY)
    first = acknowledge_open(path, LEGACY) if acknowledged else None
    load(env, "earlier-build.csv", EXAMPLE)
    assert keys(path, "transactions") == {"Netflix"}
    assert keys(path, "subscription_decisions") == {LEGACY}
    return path, first


def test_acknowledgement_survives_when_every_row_was_reimported_before_upgrade(env):
    path, first = earlier_build_reimport(env, acknowledged=True)
    ids = query(path, "SELECT id, name FROM transactions ORDER BY id")

    migrate(path)
    track(path, "Netflix")

    assert keys(path, "subscription_decisions") == {"Netflix"}
    assert query(path, "SELECT decision FROM subscription_decisions") == [("tracked",)]
    assert query(path, "SELECT merchant, charge_id FROM "
                       "subscription_price_acknowledged") == [
        ("Netflix", first.charge_id)]
    assert increases(path) == []
    assert query(path, "SELECT id, name FROM transactions ORDER BY id") == ids


def test_an_event_an_earlier_build_deleted_is_rederived_from_the_watch(env):
    path, _ = earlier_build_reimport(env, acknowledged=False)
    assert query(path, "SELECT count(*) FROM subscription_price_increases")[0][0] == 0
    assert query(path, "SELECT count(*) FROM subscription_price_watch")[0][0] == 1

    migrate(path)
    track(path, "Netflix")

    [warning] = increases(path)
    assert (warning.merchant, warning.old_cents, warning.new_cents) == (
        "Netflix", 1599, 1799)


def test_dismissed_decision_survives_when_every_row_was_reimported_first(env):
    _, path = env
    legacy_db(env)
    decide(path, LEGACY, "dismissed", "2026-09-01T00:00:00")
    load(env, "earlier-build.csv", EXAMPLE)
    assert keys(path, "subscription_decisions") == {LEGACY}

    migrate(path)

    assert query(path, "SELECT merchant, decision FROM subscription_decisions") == [
        ("Netflix", "dismissed")]
    assert "Netflix" not in recurring_section(path)


PLAID_ITEM = "item"


def plaid_rows(merchant_name):
    out = []
    for n, (day, amount) in enumerate([("2026-08-12", 15.99),
                                       ("2026-09-12", 15.99),
                                       ("2026-10-12", 17.99)]):
        out.append({"transaction_id": f"t{n}", "account_id": "acc",
                    "date": day, "name": "NETFLIX.COM", "amount": amount,
                    "merchant_name": merchant_name, "iso_currency_code": "USD",
                    "pending": False})
    return out


def save_plaid(path, rows):
    from adapters.plaid_source import to_account, to_transaction
    from core.models import ConnectionRef, Provider

    account = to_account({"account_id": "acc", "type": "credit",
                          "subtype": "credit card", "name": "Card",
                          "balances": {"current": 1.0, "iso_currency_code": "USD"}},
                         PLAID_ITEM)
    conn = store.open_db(str(path))
    try:
        user = users.ensure_local_user(conn)
        repository.save(conn, user, [account],
                        [to_transaction(r, PLAID_ITEM) for r in rows],
                        connection=ConnectionRef(provider=Provider.PLAID,
                                                 connection_id=PLAID_ITEM))
        return account.id
    finally:
        conn.close()


def test_an_explicit_plaid_merchant_name_is_not_renamed_by_the_migration(env):
    _, path = env
    account = save_plaid(path, plaid_rows("Netflix.Com"))
    track(path, LEGACY, account=account)
    assert [w.merchant for w in increases(path)] == [LEGACY]

    migrate(path)

    assert keys(path, "transactions") == {LEGACY}
    assert keys(path, "subscription_decisions") == {LEGACY}
    assert [w.merchant for w in increases(path)] == [LEGACY]

    save_plaid(path, plaid_rows("Netflix.Com"))
    assert keys(path, "transactions") == {LEGACY}
    assert [(w.merchant, w.new_cents) for w in increases(path)] == [(LEGACY, 1799)]


def test_a_plaid_descriptor_fallback_row_is_still_normalized(env):
    _, path = env
    with pytest.MonkeyPatch.context() as patch:
        patch.delitem(merchant_module.MERCHANT_PREFIXES, "NETFLIX")
        account = save_plaid(path, plaid_rows(None))
    assert keys(path, "transactions") == {LEGACY}
    track(path, LEGACY, account=account)
    [before] = increases(path)

    migrate(path)

    assert keys(path, "transactions") == {"Netflix"}
    assert keys(path, "subscription_decisions") == {"Netflix"}
    save_plaid(path, plaid_rows(None))
    assert keys(path, "transactions") == {"Netflix"}
    [after] = increases(path)
    assert (after.merchant, after.charge_id) == ("Netflix", before.charge_id)


def v11_without_the_column(path):
    from core.store import SCHEMA

    stripped = SCHEMA.replace(
        "    balance_known     INTEGER NOT NULL DEFAULT 1,\n", "").replace(
        "    CHECK (balance_known IN (0, 1)),\n", "").replace(
        "    history            TEXT,\n", "")
    assert "balance_known" not in stripped and "history" not in stripped
    con = sqlite3.connect(str(path))
    try:
        con.executescript(stripped)
        con.execute("INSERT INTO meta (id, schema_version) VALUES (1, 11)")
        con.commit()
    finally:
        con.close()


def columns(path):
    return [r[1] for r in query(path, "PRAGMA table_info(accounts)")]


def test_v12_migration_is_atomic_and_a_retry_succeeds(tmp_path, monkeypatch):
    from core import migrations

    path = tmp_path / "v11.db"
    v11_without_the_column(path)
    assert "balance_known" not in columns(path)

    def boom(conn, version):
        assert "balance_known" in [r[1] for r in conn.execute(
            "PRAGMA table_info(accounts)")]
        raise migrations.MigrationFailed("forced after the ALTER")

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "_require_referential_integrity", boom)
        conn = store.connect(str(path))
        try:
            with pytest.raises(migrations.MigrationFailed, match="forced"):
                store.init_schema(conn)
        finally:
            conn.close()

    assert "balance_known" not in columns(path)
    assert query(path, "SELECT schema_version FROM meta") == [(11,)]

    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == [12, 13]
    finally:
        conn.close()
    assert "balance_known" in columns(path)
    assert query(path, "SELECT schema_version FROM meta") == [(13,)]


def test_same_day_order_is_kept_when_two_ledgers_are_merged(env):
    _, path = env
    with pytest.MonkeyPatch.context() as patch:
        patch.delitem(merchant_module.MERCHANT_PREFIXES, "NETFLIX")
        load(env, "a.csv", [(d(9), "10.00")])
        load(env, "b.csv", [(d(10), "12.00")])
        load(env, "c.csv", [(d(10), "15.00")])
    ids = {cents: i for i, cents in query(
        path, "SELECT id, amount_cents FROM transactions")}
    ten, twelve, fifteen = ids[1000], ids[1200], ids[1500]
    acct = account_id(path)
    raw(path, "UPDATE transactions SET merchant = 'Netflix' WHERE id = ?", (twelve,))
    stamp = "2026-10-01T00:00:00"
    for merchant in (LEGACY, "Netflix"):
        raw(path, "INSERT INTO subscription_decisions VALUES (?, ?, ?, 'tracked', ?)",
            (U, acct, merchant, stamp))
        raw(path, "INSERT INTO subscription_price_watch VALUES (?, ?, ?, '2026-10-12')",
            (U, acct, merchant))
    for merchant, charge, seq in ((LEGACY, ten, 1), (LEGACY, fifteen, 2),
                                  ("Netflix", twelve, 1)):
        raw(path, "INSERT INTO subscription_price_observed VALUES (?, ?, ?, ?, ?)",
            (U, acct, merchant, charge, seq))
    raw(path, "INSERT INTO subscription_price_increases (user_id, account_id, "
              "merchant, charge_id, previous_charge_id, charge_date, old_cents, "
              "new_cents) VALUES (?, ?, 'Netflix', ?, ?, '2026-10-12', 1000, 1200)",
        (U, acct, twelve, ten))

    migrate(path)

    assert [c for (c,) in query(path, "SELECT charge_id FROM "
                                      "subscription_price_observed ORDER BY seq")
            ] == [ten, twelve, fifteen]
    expected = [("Netflix", twelve, ten, 1000, 1200)]
    sql = ("SELECT merchant, charge_id, previous_charge_id, old_cents, new_cents "
           "FROM subscription_price_increases")
    assert query(path, sql) == expected

    load(env, "again.csv", [(d(9), "10.00"), (d(10), "12.00"), (d(10), "15.00")])

    assert query(path, sql) == expected
    assert [w.charge_id for w in increases(path)] == [twelve]


def test_concurrent_upgrades_of_one_v11_file_run_the_migration_once(tmp_path):
    import threading
    import time

    from core import migrations

    path = tmp_path / "v11.db"
    v11_without_the_column(path)
    entered, release = threading.Event(), threading.Event()
    runs, results, errors = [], {}, []
    real = migrations.to_v12

    def slow_to_v12(conn):
        runs.append(1)
        real(conn)
        entered.set()
        assert release.wait(10)

    def upgrade(name):
        try:
            conn = store.connect(str(path))
            try:
                results[name] = store.init_schema(conn)
                results[name + "-version"] = conn.execute(
                    "SELECT schema_version FROM meta").fetchone()[0]
            finally:
                conn.close()
        except Exception as exc:
            errors.append(repr(exc))

    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(migrations.MIGRATIONS, 11, slow_to_v12)
        first = threading.Thread(target=upgrade, args=("first",))
        first.start()
        assert entered.wait(10)
        second = threading.Thread(target=upgrade, args=("second",))
        second.start()
        time.sleep(0.5)
        release.set()
        first.join(15)
        second.join(15)

    assert errors == []
    assert len(runs) == 1
    assert results["first"] == [12, 13] and results["second"] == []
    assert results["first-version"] == results["second-version"] == 13
    assert "balance_known" in columns(path)


def plaid_txn(tid, amount, merchant_name, pending=False, day="2026-10-12",
              name="NETFLIX.COM"):
    return {"transaction_id": tid, "account_id": "acc", "date": day,
            "name": name, "amount": amount,
            "merchant_name": merchant_name, "iso_currency_code": "USD",
            "pending": pending}


EVENT_SQL = ("SELECT merchant, charge_id, previous_charge_id, old_cents, "
             "new_cents FROM subscription_price_increases")


def ledger_order(path):
    return [c for (c,) in query(path, "SELECT charge_id FROM "
                                      "subscription_price_observed ORDER BY seq")]


def pending_then_posted_across_keys(path):
    account = save_plaid(path, [plaid_txn("t10", 10.0, "Netflix", day="2026-09-12")])
    track(path, "Netflix", account=account)
    with pytest.MonkeyPatch.context() as patch:
        patch.delitem(merchant_module.MERCHANT_PREFIXES, "NETFLIX")
        save_plaid(path, [plaid_txn("t15", 15.0, None, pending=True)])
        track(path, LEGACY, account=account)
    save_plaid(path, [plaid_txn("t12", 12.0, "Netflix")])
    with pytest.MonkeyPatch.context() as patch:
        patch.delitem(merchant_module.MERCHANT_PREFIXES, "NETFLIX")
        save_plaid(path, [plaid_txn("t15", 15.0, None)])
    ids = {cents: i for i, cents in query(
        path, "SELECT id, amount_cents FROM transactions")}
    return ids[1000], ids[1200], ids[1500]


def replay(path, twelve=12.0):
    save_plaid(path, [plaid_txn("t10", 10.0, "Netflix", day="2026-09-12"),
                      plaid_txn("t12", twelve, "Netflix"),
                      plaid_txn("t15", 15.0, None)])


def test_a_charge_that_posted_later_does_not_displace_a_recorded_predecessor(env):
    _, path = env
    ten, twelve, fifteen = pending_then_posted_across_keys(path)
    rowid = dict(query(path, "SELECT id, rowid FROM transactions"))
    assert rowid[fifteen] < rowid[twelve]
    expected = [("Netflix", twelve, ten, 1000, 1200)]
    assert query(path, EVENT_SQL) == expected
    assert keys(path, "subscription_decisions") == {LEGACY, "Netflix"}

    migrate(path)

    assert keys(path, "transactions") == {"Netflix"}
    assert sorted(ledger_order(path)) == sorted([ten, twelve, fifteen])
    assert query(path, EVENT_SQL) == expected

    replay(path)

    assert query(path, EVENT_SQL) == expected
    assert [(w.charge_id, w.old_cents, w.new_cents) for w in increases(path)] == [
        (twelve, 1000, 1200)]


def test_real_corrections_after_the_merge_still_update_or_clear_the_warning(env):
    _, path = env
    ten, twelve, _ = pending_then_posted_across_keys(path)
    migrate(path)
    replay(path)

    replay(path, twelve=13.0)
    assert query(path, EVENT_SQL) == [("Netflix", twelve, ten, 1000, 1300)]

    replay(path, twelve=9.0)
    assert query(path, EVENT_SQL) == []


def test_an_acknowledged_warning_stays_cleared_across_the_cross_key_merge(env):
    _, path = env
    _, twelve, _ = pending_then_posted_across_keys(path)
    conn = store.open_db(str(path))
    try:
        [w] = repository.load_price_increases(conn, U)
        assert repository.acknowledge_increase(conn, U, w.account_id, "Netflix",
                                               w.charge_id)
    finally:
        conn.close()
    migrate(path)
    replay(path)
    assert increases(path) == []
    assert query(path, "SELECT merchant, charge_id FROM "
                       "subscription_price_acknowledged") == [("Netflix", twelve)]


def conflicting_warnings(path):
    canonical = [plaid_txn("t10", 10.0, "Netflix", day="2026-09-12"),
                 plaid_txn("t12", 12.0, "Netflix")]
    legacy = [plaid_txn("t14", 14.0, None, day="2026-09-12"),
              plaid_txn("t16", 16.0, None)]
    account = save_plaid(path, canonical)
    track(path, "Netflix", account=account)
    with pytest.MonkeyPatch.context() as patch:
        patch.delitem(merchant_module.MERCHANT_PREFIXES, "NETFLIX")
        save_plaid(path, legacy)
        track(path, LEGACY, account=account)
    return canonical, legacy


def event_rows(path):
    return sorted(query(path, EVENT_SQL))


def test_two_warnings_with_conflicting_order_needs_each_survive_the_merge(env):
    _, path = env
    canonical, legacy = conflicting_warnings(path)
    ids = {cents: i for i, cents in query(
        path, "SELECT id, amount_cents FROM transactions")}
    expected = sorted([
        ("Netflix", ids[1200], ids[1000], 1000, 1200),
        (LEGACY, ids[1600], ids[1400], 1400, 1600),
    ])
    assert event_rows(path) == expected

    migrate(path)
    wanted = sorted([("Netflix", c, p, o, n) for _, c, p, o, n in expected])
    assert event_rows(path) == wanted

    save_plaid(path, canonical + legacy)
    assert event_rows(path) == wanted
    assert sorted(w.charge_id for w in increases(path)) == sorted(
        [ids[1200], ids[1600]])


def test_correcting_one_of_two_merged_warnings_changes_only_that_warning(env):
    _, path = env
    canonical, legacy = conflicting_warnings(path)
    ids = {cents: i for i, cents in query(
        path, "SELECT id, amount_cents FROM transactions")}
    migrate(path)
    save_plaid(path, canonical + legacy)

    corrected = [plaid_txn("t16", 18.0, None)]
    save_plaid(path, canonical + [legacy[0]] + corrected)
    assert event_rows(path) == sorted([
        ("Netflix", ids[1200], ids[1000], 1000, 1200),
        ("Netflix", ids[1600], ids[1400], 1400, 1800),
    ])

    save_plaid(path, [canonical[0], plaid_txn("t12", 9.0, "Netflix")]
               + [legacy[0]] + corrected)
    assert event_rows(path) == [("Netflix", ids[1600], ids[1400], 1400, 1800)]


def test_acknowledgements_are_kept_by_charge_id_across_the_two_ledgers(env):
    _, path = env
    canonical, legacy = conflicting_warnings(path)
    ids = {cents: i for i, cents in query(
        path, "SELECT id, amount_cents FROM transactions")}
    conn = store.open_db(str(path))
    try:
        account = account_id(path)
        assert repository.acknowledge_increase(conn, U, account, LEGACY, ids[1600])
    finally:
        conn.close()
    migrate(path)
    save_plaid(path, canonical + legacy)
    assert [w.charge_id for w in increases(path)] == [ids[1200]]
    assert query(path, "SELECT merchant, charge_id FROM "
                       "subscription_price_acknowledged") == [
        ("Netflix", ids[1600])]


def test_a_cross_key_correction_does_not_become_the_predecessor_after_the_merge(env):
    _, path = env
    ten, twelve, fifteen = pending_then_posted_across_keys(path)
    raw(path, "INSERT INTO subscription_price_corrected VALUES (?, ?, ?, ?)",
        (U, account_id(path), LEGACY, fifteen))
    expected = [("Netflix", twelve, ten, 1000, 1200)]
    assert query(path, EVENT_SQL) == expected

    migrate(path)
    assert query(path, "SELECT merchant, charge_id FROM "
                       "subscription_price_corrected") == [("Netflix", fifteen)]
    assert query(path, EVENT_SQL) == expected

    replay(path)

    assert query(path, EVENT_SQL) == expected
    assert [w.charge_id for w in increases(path)] == [twelve]


def v12_without_history(path):
    from core.store import SCHEMA

    stripped = SCHEMA.replace("    history            TEXT,\n", "")
    assert "history" not in stripped and "balance_known" in stripped
    con = sqlite3.connect(str(path))
    try:
        con.executescript(stripped)
        con.execute("INSERT INTO meta (id, schema_version) VALUES (1, 12)")
        con.commit()
    finally:
        con.close()


def increase_columns(path):
    return [r[1] for r in query(path, "PRAGMA table_info(subscription_price_increases)")]


def test_an_earlier_v12_file_without_history_upgrades_and_then_saves(env):
    _, path = env
    v12_without_history(path)
    assert "history" not in increase_columns(path)

    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == [13]
    finally:
        conn.close()
    assert "history" in increase_columns(path)
    assert query(path, "SELECT schema_version FROM meta") == [(13,)]

    account = save_plaid(path, [plaid_txn("t10", 10.0, "Netflix", day="2026-09-12"),
                                plaid_txn("t12", 12.0, "Netflix")])
    track(path, "Netflix", account=account)
    assert [w.new_cents for w in increases(path)] == [1200]

    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == []
    finally:
        conn.close()
    assert [w.new_cents for w in increases(path)] == [1200]


def test_the_v12_to_v13_step_is_atomic_and_a_retry_succeeds(env, monkeypatch):
    from core import migrations

    _, path = env
    v12_without_history(path)

    def boom(conn, version):
        assert "history" in [r[1] for r in conn.execute(
            "PRAGMA table_info(subscription_price_increases)")]
        raise migrations.MigrationFailed("forced after the ALTER")

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "_require_referential_integrity", boom)
        conn = store.connect(str(path))
        try:
            with pytest.raises(migrations.MigrationFailed, match="forced"):
                store.init_schema(conn)
        finally:
            conn.close()
    assert "history" not in increase_columns(path)
    assert query(path, "SELECT schema_version FROM meta") == [(12,)]

    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == [13]
    finally:
        conn.close()
    assert "history" in increase_columns(path)


LEGACY_CA = "Netflix.Com Ca"
CA_NAME = "NETFLIX.COM CA"
CANONICAL = [plaid_txn("t10", 10.0, "Netflix", day="2026-09-12"),
             plaid_txn("t12", 12.0, "Netflix")]
COM = [plaid_txn("c11", 11.0, None, day="2026-08-12")]
CA_BEFORE = [plaid_txn("c15", 15.0, None, day="2026-10-20", name=CA_NAME)]
CA_AFTER = [plaid_txn("c15", 15.0, None, day="2026-09-25", name=CA_NAME)]


def three_key_db(path, order):
    account = save_plaid(path, CANONICAL)
    track(path, "Netflix", account=account)

    def com():
        save_plaid(path, COM)
        track(path, LEGACY, account=account)

    def ca():
        save_plaid(path, CA_BEFORE)
        track(path, LEGACY_CA, account=account)
        save_plaid(path, CA_AFTER)

    steps = {"com": com, "ca": ca}
    with pytest.MonkeyPatch.context() as patch:
        patch.delitem(merchant_module.MERCHANT_PREFIXES, "NETFLIX")
        for name in order:
            steps[name]()
    ids = {cents: i for i, cents in query(
        path, "SELECT id, amount_cents FROM transactions")}
    return account, ids


def replay_three(path, twelve=12.0, extra=()):
    save_plaid(path, [CANONICAL[0], plaid_txn("t12", twelve, "Netflix")]
               + COM + CA_AFTER + list(extra))


ORDERS = [("com", "ca"), ("ca", "com")]


@pytest.mark.parametrize("order", ORDERS)
def test_three_aliases_keep_the_canonical_warning_whatever_the_alias_order(env, order):
    _, path = env
    _, ids = three_key_db(path, order)
    expected = [("Netflix", ids[1200], ids[1000], 1000, 1200)]
    assert query(path, EVENT_SQL) == expected
    assert keys(path, "transactions") == {"Netflix", LEGACY, LEGACY_CA}
    assert query(path, "SELECT merchant, charge_id FROM "
                       "subscription_price_corrected") == [(LEGACY_CA, ids[1500])]

    migrate(path)
    assert keys(path, "transactions") == {"Netflix"}
    assert keys(path, "subscription_decisions") == {"Netflix"}
    assert query(path, EVENT_SQL) == expected

    replay_three(path)
    assert query(path, EVENT_SQL) == expected
    assert [w.charge_id for w in increases(path)] == [ids[1200]]

    store.open_db(str(path)).close()
    assert query(path, EVENT_SQL) == expected


@pytest.mark.parametrize("order", ORDERS)
def test_post_upgrade_correction_later_increase_and_acknowledgement(env, order):
    _, path = env
    account, ids = three_key_db(path, order)
    conn = store.open_db(str(path))
    try:
        assert repository.acknowledge_increase(conn, U, account, "Netflix",
                                               ids[1200])
    finally:
        conn.close()
    migrate(path)
    replay_three(path)
    assert increases(path) == []
    assert query(path, "SELECT merchant, charge_id FROM "
                       "subscription_price_acknowledged") == [
        ("Netflix", ids[1200])]

    later = plaid_txn("t14", 14.0, "Netflix", day="2026-11-12")
    replay_three(path, extra=[later])
    new_id = [i for i, c in query(
        path, "SELECT id, amount_cents FROM transactions") if c == 1400][0]
    assert [(w.charge_id, w.old_cents, w.new_cents) for w in increases(path)] == [
        (new_id, 1200, 1400)]
    assert (("Netflix", ids[1200], ids[1000], 1000, 1200)
            in query(path, EVENT_SQL))


@pytest.mark.parametrize("order", ORDERS)
def test_real_correction_after_the_group_merge_updates_then_clears(env, order):
    _, path = env
    _, ids = three_key_db(path, order)
    migrate(path)
    replay_three(path)

    replay_three(path, twelve=13.0)
    assert query(path, EVENT_SQL) == [("Netflix", ids[1200], ids[1000], 1000, 1300)]

    replay_three(path, twelve=9.0)
    assert query(path, EVENT_SQL) == []


@pytest.mark.parametrize("order", ORDERS)
def test_a_true_v11_file_without_history_survives_the_three_key_merge(env, order):
    _, path = env
    _, ids = three_key_db(path, order)
    raw(path, "ALTER TABLE subscription_price_increases DROP COLUMN history")
    assert "history" not in increase_columns(path)
    # the foreign-key pragma stays on for this connection only; reopen as v11
    expected = [("Netflix", ids[1200], ids[1000], 1000, 1200)]
    migrate(path)
    assert "history" in increase_columns(path)
    replay_three(path)
    assert query(path, EVENT_SQL) == expected


@pytest.mark.parametrize("order", ORDERS)
def test_foreign_corrections_exclude_only_keys_the_warning_did_not_own(env, order):
    import json

    def history(path):
        [(raw_history,)] = query(path, "SELECT history FROM "
                                       "subscription_price_increases")
        return json.loads(raw_history)

    _, exclusive = env
    _, ids = three_key_db(exclusive, order)
    migrate(exclusive)
    assert history(exclusive)["foreign"] == [ids[1500]]
    assert history(exclusive)["seen"] == [ids[1000], ids[1200]]

    shared_dir = exclusive.parent / "shared"
    shared_dir.mkdir()
    shared = shared_dir / "s.db"
    _, ids = three_key_db(shared, order)
    raw(shared, "INSERT INTO subscription_price_corrected VALUES (?, ?, 'Netflix', ?)",
        (U, account_id(shared), ids[1500]))
    migrate(shared)
    assert history(shared)["foreign"] == []
