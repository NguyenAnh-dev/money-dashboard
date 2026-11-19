from __future__ import annotations

import sqlite3
from io import StringIO

import pytest
from rich.console import Console

import import_csv
import main as app
import subscriptions
from adapters.fake import FakeSource
from core import migrations, repository, store, users

NETFLIX = (
    "Date,Description,Amount\n"
    "08/12/2026,NETFLIX.COM,-15.99\n"
    "09/12/2026,NETFLIX.COM,-15.99\n"
    "10/12/2026,NETFLIX.COM,-17.99\n"
)

MERCHANT = "Netflix.Com"
OTHER_USER = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "subs.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("DATA_SOURCE", "fake")
    statement = tmp_path / "netflix.csv"
    statement.write_text(NETFLIX, encoding="utf-8")
    return path, str(statement)


def run(capsys, *argv):
    code = subscriptions.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def sql(path, query, *args):
    con = sqlite3.connect(str(path))
    try:
        return con.execute(query, args).fetchall()
    finally:
        con.close()


def two_cards(env):
    path, statement = env
    import_csv.import_csv(statement, "card-a")
    import_csv.import_csv(statement, "card-b")
    ids = {name: ident for ident, name in sql(path, "SELECT id, name FROM accounts")}
    assert set(ids) == {"card-a", "card-b"}
    return path, statement, ids


def render(path):
    buffer = StringIO()
    app.main(console=Console(file=buffer, width=120, legacy_windows=False,
                             no_color=True, highlight=False))
    return buffer.getvalue()


def recurring_section(path):
    return render(path).split("Recurring Charges")[1].split("Spending by Category")[0]


def statuses(capsys):
    code, out, _ = run(capsys, "list")
    assert code == 0
    result = {}
    for line in out.splitlines():
        account = "card-a" if "account card-a" in line else "card-b"
        result[account] = line.split()[0]
    assert result, "list printed no candidates, so the comparisons below prove nothing"
    return result


def test_a_fresh_database_lists_both_cards_as_candidates(env, capsys):
    two_cards(env)
    assert statuses(capsys) == {"card-a": "candidate", "card-b": "candidate"}


def test_track_dismiss_restore_change_only_the_named_card(env, capsys):
    path, _, ids = two_cards(env)

    assert run(capsys, "track", "--account", ids["card-a"], "--merchant", MERCHANT)[0] == 0
    assert statuses(capsys) == {"card-a": "tracked", "card-b": "candidate"}

    assert run(capsys, "dismiss", "--account", ids["card-b"], "--merchant", MERCHANT)[0] == 0
    assert statuses(capsys) == {"card-a": "tracked", "card-b": "dismissed"}

    assert run(capsys, "dismiss", "--account", ids["card-a"], "--merchant", MERCHANT)[0] == 0
    assert run(capsys, "restore", "--account", ids["card-a"], "--merchant", MERCHANT)[0] == 0
    assert statuses(capsys) == {"card-a": "candidate", "card-b": "dismissed"}
    assert sql(path, "SELECT count(*) FROM subscription_decisions")[0][0] == 1


def test_decisions_survive_reimport_and_dismissed_stays_hidden_until_restored(env, capsys):
    path, statement, ids = two_cards(env)
    run(capsys, "track", "--account", ids["card-a"], "--merchant", MERCHANT)
    run(capsys, "dismiss", "--account", ids["card-b"], "--merchant", MERCHANT)
    before = sql(path, "SELECT * FROM subscription_decisions ORDER BY account_id")
    txn_ids = sql(path, "SELECT id FROM transactions ORDER BY id")
    assert len(before) == 2 and txn_ids

    assert import_csv.import_csv(statement, "card-a").result.new == 0
    assert import_csv.import_csv(statement, "card-b").result.new == 0

    assert sql(path, "SELECT * FROM subscription_decisions ORDER BY account_id") == before
    assert sql(path, "SELECT id FROM transactions ORDER BY id") == txn_ids
    assert statuses(capsys) == {"card-a": "tracked", "card-b": "dismissed"}

    section = recurring_section(path)
    assert f"{MERCHANT} (tracked)" in section
    assert "card-a" in section
    assert "card-b" not in section

    run(capsys, "restore", "--account", ids["card-b"], "--merchant", MERCHANT)
    assert "card-b" in recurring_section(path)


def test_a_decision_survives_a_month_going_missing(env, capsys):
    path, _, ids = two_cards(env)
    run(capsys, "dismiss", "--account", ids["card-a"], "--merchant", MERCHANT)
    con = sqlite3.connect(str(path))
    con.execute("DELETE FROM transactions WHERE account_id = ? AND date = '2026-09-12'",
                (ids["card-a"],))
    con.commit()
    con.close()

    _, out, _ = run(capsys, "list")
    assert "no longer detected" in out
    assert sql(path, "SELECT decision FROM subscription_decisions") == [("dismissed",)]

    assert run(capsys, "restore", "--account", ids["card-a"], "--merchant", MERCHANT)[0] == 0
    assert sql(path, "SELECT count(*) FROM subscription_decisions")[0][0] == 0


def test_bad_requests_are_rejected_and_store_nothing(env, capsys):
    path, _, ids = two_cards(env)
    for args in (
        ("track", "--account", "nope", "--merchant", MERCHANT),
        ("track", "--account", ids["card-a"], "--merchant", "Hulu"),
        ("restore", "--account", ids["card-a"], "--merchant", MERCHANT),
    ):
        code, _, err = run(capsys, *args)
        assert code == 1 and "FAILED" in err
    assert sql(path, "SELECT count(*) FROM subscription_decisions")[0][0] == 0


def test_another_users_account_cannot_be_decided(env):
    path, _, ids = two_cards(env)
    conn = store.open_db(str(path))
    try:
        users.create_user_with_hash(conn, "other@example.com",
                                    users.UNUSABLE_PASSWORD_HASH,
                                    user_id=OTHER_USER)
        with pytest.raises(repository.UnknownAccount):
            repository.set_decision(conn, OTHER_USER, ids["card-a"],
                                    MERCHANT, repository.TRACKED)
    finally:
        conn.close()
    assert sql(path, "SELECT count(*) FROM subscription_decisions")[0][0] == 0


def _seeded(path):
    conn = store.open_db(str(path))
    try:
        user = users.ensure_local_user(conn)
        source = FakeSource()
        repository.save(conn, user, source.fetch_accounts(),
                        source.fetch_transactions().added,
                        connection=source.connection())
    finally:
        conn.close()
    return path


def _downgrade(path, version):
    con = sqlite3.connect(str(path))
    con.execute("DROP TABLE subscription_decisions")
    con.execute("UPDATE meta SET schema_version = ?", (version,))
    con.commit()
    con.close()


def test_a_v7_database_upgrades_keeps_ids_and_accepts_decisions(tmp_path):
    path = _seeded(tmp_path / "old.db")
    _downgrade(path, 7)
    ids = sql(path, "SELECT id FROM transactions ORDER BY id")
    assert ids

    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == [8]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        account = conn.execute("SELECT id FROM accounts LIMIT 1").fetchone()[0]
        repository.set_decision(conn, users.LOCAL_USER_ID, account, "Hulu",
                                repository.TRACKED)
    finally:
        conn.close()

    assert sql(path, "SELECT id FROM transactions ORDER BY id") == ids
    assert sql(path, "SELECT decision FROM subscription_decisions") == [("tracked",)]
    assert sql(path, "SELECT schema_version FROM meta")[0][0] == store.SCHEMA_VERSION


def test_the_supported_v6_chain_reaches_v8_with_ids_intact(tmp_path):
    path = _seeded(tmp_path / "v6.db")
    _downgrade(path, 6)
    count = sql(path, "SELECT count(*) FROM transactions")[0][0]
    assert count > 0

    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == [7, 8]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()

    assert sql(path, "SELECT count(*) FROM transactions")[0][0] == count
    joined = sql(path, "SELECT count(*) FROM transactions t JOIN accounts a "
                       "ON a.id = t.account_id")[0][0]
    assert joined == count
    assert sql(path, "SELECT schema_version FROM meta")[0][0] == 8


def test_the_registry_has_a_step_for_v8():
    assert 7 in migrations.MIGRATIONS


def test_track_is_refused_while_dismissed_and_allowed_after_restore(env, capsys):
    path, _, ids = two_cards(env)
    a, b = ids["card-a"], ids["card-b"]

    run(capsys, "dismiss", "--account", a, "--merchant", MERCHANT)
    run(capsys, "track", "--account", b, "--merchant", MERCHANT)
    code, _, err = run(capsys, "track", "--account", a, "--merchant", MERCHANT)
    assert code == 1 and "restore" in err
    assert statuses(capsys) == {"card-a": "dismissed", "card-b": "tracked"}
    assert "card-a" not in recurring_section(path)

    assert run(capsys, "restore", "--account", a, "--merchant", MERCHANT)[0] == 0
    assert run(capsys, "track", "--account", a, "--merchant", MERCHANT)[0] == 0
    assert statuses(capsys) == {"card-a": "tracked", "card-b": "tracked"}


def test_the_dismissed_guard_lives_in_the_write_itself(env):
    path, _, ids = two_cards(env)
    conn = store.open_db(str(path))
    try:
        repository.set_decision(conn, users.LOCAL_USER_ID, ids["card-a"],
                                MERCHANT, repository.DISMISSED)
        with pytest.raises(repository.DecisionDismissed):
            repository.set_decision(conn, users.LOCAL_USER_ID, ids["card-a"],
                                    MERCHANT, repository.TRACKED)
    finally:
        conn.close()
    assert sql(path, "SELECT decision FROM subscription_decisions") == [("dismissed",)]


def test_unicode_merchant_survives_a_cp1252_terminal(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    merchant = "商店"
    statement = tmp_path / "shop.csv"
    statement.write_text(NETFLIX.replace("NETFLIX.COM", merchant), encoding="utf-8")
    db = tmp_path / "u.db"
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("PLAID")}
    env.update(DB_PATH=str(db), DATA_SOURCE="fake", PYTHONIOENCODING="cp1252",
               PYTHONUTF8="0")

    def cli(*args):
        return subprocess.run([sys.executable, *args], cwd=root, env=env,
                              capture_output=True, text=True, encoding="cp1252")

    imported = cli("import_csv.py", str(statement), "--account", "card-u", "--yes")
    assert imported.returncode == 0, imported.stderr

    listed = cli("subscriptions.py", "list")
    assert listed.returncode == 0, listed.stderr
    assert "candidate" in listed.stdout

    stored = sql(db, "SELECT DISTINCT merchant FROM transactions")[0][0]
    account = sql(db, "SELECT id FROM accounts")[0][0]
    dismissed = cli("subscriptions.py", "dismiss", "--account", account,
                    "--merchant", stored)
    assert dismissed.returncode == 0, dismissed.stderr
    assert sql(db, "SELECT merchant, decision FROM subscription_decisions") == [
        (stored, "dismissed")]
