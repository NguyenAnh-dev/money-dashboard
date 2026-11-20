from __future__ import annotations

import sqlite3

import pytest

import subscriptions
from core import repository, store, users
from tests.test_price_increases import (
    EXAMPLE, FLAT3, MERCHANT, U, account_id, d, env, increases, load, query,
    render, section, track,
)
from tests.test_subscriptions import _downgrade, _seeded


OTHER = "22222222-2222-4222-8222-222222222222"


def cli(capsys, *argv):
    code = subscriptions.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def ack(capsys, path, charge, account=None, merchant=MERCHANT):
    return cli(capsys, "acknowledge", "--account", account or account_id(path),
               "--merchant", merchant, "--charge", charge)


def stored(path):
    return query(path, "SELECT charge_id FROM subscription_price_acknowledged")


def test_acknowledging_clears_the_warning_and_twice_is_harmless(env, capsys):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    [warning] = increases(path)
    assert "+$2.00" in section(path)

    code, out, _ = ack(capsys, path, warning.charge_id)
    assert code == 0 and out.startswith("acknowledged:")
    assert increases(path) == []
    assert stored(path) == [(warning.charge_id,)]
    assert "Price Increases" not in section_or_empty(path)

    code, out, _ = ack(capsys, path, warning.charge_id)
    assert code == 0 and out.startswith("already acknowledged:")
    assert stored(path) == [(warning.charge_id,)]
    assert query(path, "SELECT count(*) FROM subscription_price_increases")[0][0] == 1


def section_or_empty(path):
    return render(path)


def test_it_stays_cleared_after_reopen_and_reimport_and_a_later_rise_is_separate(env, capsys):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    [first] = increases(path)
    ack(capsys, path, first.charge_id)

    load(env, "a.csv", EXAMPLE)
    load(env, "b.csv", [(d(11), "17.99")])
    assert increases(path) == []

    load(env, "c.csv", [(d(12), "19.99")])
    [second] = increases(path)
    assert second.charge_id != first.charge_id
    assert (second.old_cents, second.new_cents) == (1799, 1999)
    assert "Price Increases" in section_or_empty(path)

    ack(capsys, path, second.charge_id)
    assert increases(path) == []
    assert len(stored(path)) == 2
    assert len(query(path, "SELECT * FROM subscription_price_increases")) == 2


def test_acknowledging_one_event_leaves_another_open(env, capsys):
    _, path = env
    load(env, "a.csv", FLAT3)
    track(path)
    load(env, "b.csv", [(d(11), "17.99"), (d(12), "19.99")])
    first, second = increases(path)[::-1]
    ack(capsys, path, first.charge_id)
    assert [w.charge_id for w in increases(path)] == [second.charge_id]


def test_unknown_charge_wrong_account_and_untracked_are_refused(env, capsys):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    [w] = increases(path)
    assert ack(capsys, path, "nope")[0] == 1
    assert ack(capsys, path, w.charge_id, account="missing")[0] == 1
    assert ack(capsys, path, w.charge_id, merchant="Hulu")[0] == 1
    assert stored(path) == []
    assert increases(path) == [w]


def test_dismiss_hides_the_open_warning_and_blocks_acknowledgement(env, capsys):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    [w] = increases(path)
    acct = account_id(path)
    assert "+$2.00" in render(path)

    assert cli(capsys, "dismiss", "--account", acct, "--merchant", MERCHANT)[0] == 0
    assert cli(capsys, "warnings")[1].strip() == "no price increases"
    assert "Price Increases" not in render(path)
    assert increases(path) == []
    code, _, err = ack(capsys, path, w.charge_id)
    assert code == 1 and "FAILED" in err
    assert stored(path) == []


def test_restore_after_acknowledging_starts_fresh(env, capsys):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    [w] = increases(path)
    ack(capsys, path, w.charge_id)
    assert stored(path) == [(w.charge_id,)]
    acct = account_id(path)
    assert cli(capsys, "restore", "--account", acct, "--merchant", MERCHANT)[0] == 0
    assert stored(path) == []
    track(path)
    assert [x.charge_id for x in increases(path)] == [w.charge_id]


def test_a_restore_between_the_check_and_the_write_cannot_be_acknowledged(
        env, monkeypatch):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    [w] = increases(path)
    acct = account_id(path)
    other = store.open_db(str(path))

    class RestoreOnClock:
        @staticmethod
        def now():
            repository.clear_decision(other, U, acct, MERCHANT)
            return __import__("datetime").datetime(2026, 1, 1)

    monkeypatch.setattr(repository, "datetime", RestoreOnClock)
    conn = store.open_db(str(path))
    try:
        with pytest.raises(LookupError):
            repository.acknowledge_increase(conn, U, acct, MERCHANT, w.charge_id)
    finally:
        conn.close()
        other.close()
    monkeypatch.undo()
    assert stored(path) == []
    track(path)
    assert [x.charge_id for x in increases(path)] == [w.charge_id]


def test_the_printed_selector_acknowledges_a_merchant_with_hidden_characters(
        env, capsys):
    _, path = env
    load(env, "a.csv", EXAMPLE, description="NET\u202EFLIX.COM")
    merchant = query(path, "SELECT DISTINCT merchant FROM transactions")[0][0]
    assert "\u202E" in merchant
    track(path, merchant=merchant)
    code, out, _ = cli(capsys, "warnings")
    assert code == 0 and "\u202E" not in out
    token = out.split("[event: ")[1].split("]")[0]
    assert token.isascii() and " " not in token

    code, out, _ = cli(capsys, "acknowledge", "--event", token)
    assert code == 0 and out.startswith("acknowledged:") and "\u202E" not in out
    assert len(stored(path)) == 1
    assert cli(capsys, "acknowledge", "--event", token)[1].startswith(
        "already acknowledged")
    assert cli(capsys, "warnings")[1].strip() == "no price increases"
    assert cli(capsys, "acknowledge", "--event", "garbage!")[0] == 1
    assert cli(capsys, "acknowledge", "--event", token, "--charge", "x")[0] == 1


def test_a_selector_with_an_unpaired_surrogate_is_rejected_cleanly(env, capsys):
    import base64
    import json

    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    [w] = increases(path)
    acct = account_id(path)
    for fields in ([acct, "Net\ud800flix", w.charge_id],
                   ["\ud800", MERCHANT, w.charge_id],
                   [acct, MERCHANT, "x\udfff"]):
        raw = json.dumps(fields, ensure_ascii=True).encode("ascii")
        assert b"\ud" in raw
        token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        code, out, err = cli(capsys, "acknowledge", "--event", token)
        assert code == 1 and out == ""
        assert "not a valid event selector" in err
        assert "Traceback" not in err
    assert stored(path) == []
    assert increases(path) == [w]


def test_warnings_command_lists_only_open_ones(env, capsys):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    [w] = increases(path)
    code, out, _ = cli(capsys, "warnings")
    assert code == 0 and "[event: " in out and "$15.99 -> $17.99" in out
    ack(capsys, path, w.charge_id)
    assert cli(capsys, "warnings")[1].strip() == "no price increases"


def test_another_user_cannot_acknowledge(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    [w] = increases(path)
    conn = store.open_db(str(path))
    try:
        users.create_user_with_hash(conn, "o@example.com",
                                    users.UNUSABLE_PASSWORD_HASH, user_id=OTHER)
        with pytest.raises(repository.UnknownAccount):
            repository.acknowledge_increase(conn, OTHER, account_id(path),
                                            MERCHANT, w.charge_id)
    finally:
        conn.close()
    assert stored(path) == []


@pytest.mark.parametrize("version,chain", [(7, [8, 9, 10, 11, 12, 13]), (6, [7, 8, 9, 10, 11, 12, 13])])
def test_old_files_upgrade_and_accept_acknowledgements(tmp_path, version, chain):
    path = _seeded(tmp_path / "old.db")
    _downgrade(path, version)
    con = sqlite3.connect(str(path))
    con.execute("DROP TABLE subscription_price_acknowledged")
    con.commit()
    con.close()
    ids = query(path, "SELECT id FROM transactions ORDER BY id")
    assert ids

    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == chain
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        acct = conn.execute("SELECT id FROM accounts LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO subscription_price_acknowledged VALUES (?, ?, 'X', 'c', "
            "'2026-01-01T00:00:00')", (users.LOCAL_USER_ID, acct))
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    after = query(path, "SELECT id FROM transactions ORDER BY id")
    if version == 7:
        assert after == ids
    else:
        assert len(after) == len(ids) and len(set(after)) == len(after)


def test_a_fresh_database_has_the_table(tmp_path):
    conn = store.open_db(str(tmp_path / "f.db"))
    try:
        assert conn.execute("SELECT count(*) FROM subscription_price_acknowledged"
                            ).fetchone()[0] == 0
    finally:
        conn.close()
