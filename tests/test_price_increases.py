from __future__ import annotations

import sqlite3
from datetime import date
from io import StringIO

import pytest
from rich.console import Console

import import_csv
import main as app
import subscriptions
from core import repository, store, users

U = users.LOCAL_USER_ID
ACCT = "card"
MERCHANT = "Netflix"


def stmt(tmp_path, name, rows, description="NETFLIX.COM"):
    path = tmp_path / name
    lines = ["Date,Description,Amount"]
    lines += [f'{d},"{description}",-{amt}' for d, amt in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "p.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("DATA_SOURCE", "fake")
    for name in ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ENV", "PLAID_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path, path


def load(env, name, rows, account=ACCT, description="NETFLIX.COM"):
    return import_csv.import_csv(stmt(env[0], name, rows, description), account)


def query(path, sql):
    con = sqlite3.connect(str(path))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def account_id(path):
    return query(path, "SELECT id FROM accounts")[0][0]


def track(path, merchant=MERCHANT, account=None):
    conn = store.open_db(str(path))
    try:
        repository.set_decision(conn, U, account or account_id(path), merchant,
                                repository.TRACKED)
    finally:
        conn.close()


def increases(path):
    conn = store.open_db(str(path))
    try:
        return repository.load_price_increases(conn, U)
    finally:
        conn.close()


def summary(path):
    return [(w.date, w.old_cents, w.new_cents) for w in increases(path)]


def render(path):
    buffer = StringIO()
    app.main(console=Console(file=buffer, width=140, legacy_windows=False,
                             no_color=True, highlight=False))
    return buffer.getvalue()


def section(path):
    return render(path).split("Price Increases")[1].split("Recurring Charges")[0]


def d(m, day=12):
    return f"{m:02d}/{day:02d}/2026"


FLAT3 = [(d(8), "15.99"), (d(9), "15.99"), (d(10), "15.99")]
EXAMPLE = [(d(8), "15.99"), (d(9), "15.99"), (d(10), "17.99")]


def test_initial_tracking_compares_latest_with_its_predecessor_only(env):
    _, path = env
    load(env, "a.csv", [(d(7), "15.99"), (d(8), "15.99"), (d(9), "17.99"), (d(10), "17.99")])
    assert query(path, "SELECT count(*) FROM transactions")[0][0] == 4
    track(path)
    assert summary(path) == []


def test_the_example_gives_one_dated_warning_that_flat_follow_ups_keep(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    first = increases(path)
    assert [(w.date, w.old_cents, w.new_cents, w.delta_cents) for w in first] == [
        (date(2026, 10, 12), 1599, 1799, 200)]

    load(env, "b.csv", [(d(11), "17.99")])
    assert increases(path) == first
    load(env, "b.csv", [(d(11), "17.99")])
    assert increases(path) == first
    text = render(path)
    assert "Price Increases" in text
    assert "$15.99" in text and "$17.99" in text and "+$2.00" in text
    assert "2026-10-12" in text and "Netflix" in text


def test_two_increases_imported_together_are_two_events(env):
    _, path = env
    load(env, "a.csv", FLAT3)
    track(path)
    assert summary(path) == []
    load(env, "b.csv", [(d(11), "17.99"), (d(12), "19.99")])
    assert summary(path) == [(date(2026, 12, 12), 1799, 1999),
                             (date(2026, 11, 12), 1599, 1799)]
    assert len({w.charge_id for w in increases(path)}) == 2


def test_a_later_decrease_leaves_the_earlier_warning_and_adds_none(env):
    _, path = env
    load(env, "a.csv", FLAT3)
    track(path)
    load(env, "b.csv", [(d(11), "17.99")])
    before = increases(path)
    assert len(before) == 1
    load(env, "c.csv", [(d(12), "15.99")])
    assert increases(path) == before


def test_return_to_the_old_price_after_a_decrease_is_a_new_event(env):
    _, path = env
    load(env, "a.csv", FLAT3)
    track(path)
    load(env, "b.csv", [(d(11), "13.99")])
    assert summary(path) == []
    load(env, "c.csv", [(d(12), "15.99")])
    assert summary(path) == [(date(2026, 12, 12), 1399, 1599)]


def test_older_backfill_repeating_rows_adds_nothing(env):
    _, path = env
    load(env, "a.csv", FLAT3)
    track(path)
    before = query(path, "SELECT count(*) FROM transactions")[0][0]
    run = load(env, "b.csv", [(d(7), "13.99"), (d(8), "15.99"), (d(9), "15.99")])
    assert run.result.new == 1 and run.result.existing == 2
    assert query(path, "SELECT count(*) FROM transactions")[0][0] == before + 1
    assert summary(path) == []


def test_pending_charge_never_creates_a_warning(env):
    _, path = env
    load(env, "a.csv", FLAT3)
    track(path)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM transactions LIMIT 1").fetchone()
    values = dict(row)
    values.update(id="pending-x", date="2026-11-12", amount_cents=1799, pending=1)
    cols = list(values)
    con.execute(f"INSERT INTO transactions ({', '.join(cols)}) VALUES "
                f"({', '.join('?' for _ in cols)})", [values[c] for c in cols])
    con.commit()
    con.close()
    conn = store.open_db(str(path))
    try:
        with conn:
            repository.scan_price_increases(conn, U)
    finally:
        conn.close()
    assert query(path, "SELECT count(*) FROM transactions WHERE pending = 1")[0][0] == 1
    assert summary(path) == []


def test_a_warning_survives_the_candidate_heuristic_failing(env):
    _, path = env
    load(env, "a.csv", [(d(7), "15.99"), (d(8), "15.99"), (d(9), "17.99")])
    conn = store.open_db(str(path))
    try:
        assert any(c.merchant == MERCHANT for c in subscriptions.candidates(conn, U))
    finally:
        conn.close()
    track(path)
    before = increases(path)
    assert len(before) == 1
    load(env, "b.csv", [(d(11), "17.99")])
    conn = store.open_db(str(path))
    try:
        assert not any(c.merchant == MERCHANT for c in subscriptions.candidates(conn, U))
    finally:
        conn.close()
    assert increases(path) == before
    assert "Price Increases" in render(path)


def test_dismiss_hides_and_restore_clears_everything(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    assert len(increases(path)) == 1
    acct = account_id(path)
    conn = store.open_db(str(path))
    try:
        repository.set_decision(conn, U, acct, MERCHANT, repository.DISMISSED)
        assert repository.load_price_increases(conn, U) == []
        assert "Price Increases" not in render(path)
        assert repository.clear_decision(conn, U, acct, MERCHANT)
        assert conn.execute("SELECT count(*) FROM subscription_price_increases").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM subscription_price_observed").fetchone()[0] == 0
    finally:
        conn.close()


def test_two_cards_are_independent(env):
    _, path = env
    load(env, "a.csv", EXAMPLE, account="card-a")
    load(env, "b.csv", EXAMPLE, account="card-b")
    ids = {n: i for i, n in query(path, "SELECT id, name FROM accounts")}
    assert set(ids) == {"card-a", "card-b"}
    track(path, account=ids["card-a"])
    assert [w.account_id for w in increases(path)] == [ids["card-a"]]


def test_control_and_bidi_characters_are_escaped_but_stored_raw(env):
    _, path = env
    nasty = "Evil\u202ECorp\x1b[31m"
    load(env, "n.csv", EXAMPLE, description=nasty)
    stored = query(path, "SELECT DISTINCT merchant FROM transactions")[0][0]
    assert "\u202E" in stored and "\x1b" in stored
    track(path, stored)
    text = section(path)
    assert "\u202E" not in text and "\x1b" not in text
    assert "\\u202e" in text and "\\x1b" in text
    assert "$15.99" in text and "$17.99" in text and "+$2.00" in text
    assert increases(path)[0].merchant == stored


def test_cli_end_to_end_track_then_later_imports(env, capsys):
    tmp_path, path = env
    first = stmt(tmp_path, "a.csv", EXAMPLE)
    assert import_csv.main([first, "--account", ACCT, "--yes"]) == 0
    acct = account_id(path)
    assert subscriptions.main(["track", "--account", acct, "--merchant", MERCHANT]) == 0
    capsys.readouterr()
    assert section(path).count("+$2.00") == 1

    later = stmt(tmp_path, "b.csv", [(d(10), "17.99"), (d(11), "17.99")])
    assert import_csv.main([later, "--account", ACCT, "--yes"]) == 0
    assert import_csv.main([later, "--account", ACCT, "--yes"]) == 0
    assert summary(path) == [(date(2026, 10, 12), 1599, 1799)]
    assert section(path).count("+$2.00") == 1

    third = stmt(tmp_path, "c.csv", [(d(12), "19.99")])
    assert import_csv.main([third, "--account", ACCT, "--yes"]) == 0
    assert summary(path) == [(date(2026, 12, 12), 1799, 1999),
                             (date(2026, 10, 12), 1599, 1799)]
    text = section(path)
    assert text.count("+$2.00") == 2
    assert "2026-12-12" in text and "2026-10-12" in text
    assert "$19.99" in text


def test_v8_database_upgrades_and_still_tracks(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    con = sqlite3.connect(str(path))
    con.execute("DROP TABLE subscription_price_increases")
    con.execute("DROP TABLE subscription_price_observed")
    con.execute("DROP TABLE subscription_price_watch")
    con.execute("UPDATE meta SET schema_version = 8")
    con.commit()
    ids = con.execute("SELECT id FROM transactions ORDER BY id").fetchall()
    con.close()
    assert ids
    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == [9, 10, 11, 12, 13]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    assert query(path, "SELECT id FROM transactions ORDER BY id") == ids
    track(path)
    assert len(increases(path)) == 1
