"""
tests/test_migrations.py
------------------------
Step 7: a v6 database is walked forward instead of being thrown away.

The whole reason this exists is that "delete the file and re-sync" stopped being
cheap at 6.3. A re-sync rebuilds transactions from the source; it cannot rebuild
a linked Plaid Item, because that needs a browser and a human. So a schema bump
now costs every user every bank connection, and that price is only acceptable
once.

The v6 -> v7 step rekeys the fixture and CSV connections to their owner, which
means it rewrites PRIMARY KEYS on two tables and the foreign key between them.
That is the dangerous kind of migration, so most of what is below is about the
failure modes rather than the happy path: a half-rekeyed file, an orphaned
transaction, a step that raises in the middle.
"""

from __future__ import annotations

import sqlite3
from io import StringIO

import pytest
from rich.console import Console

import main as app
from adapters.fake import CONNECTION_ID, FakeSource
from core import migrations, repository, store, users
from core.models import scoped_connection_id

USER = users.LOCAL_USER_ID


def _seed(path, user_id, source, version=None):
    conn = store.open_db(str(path))
    try:
        users.ensure_local_user(conn)
        if user_id != USER:
            users.create_user_with_hash(
                conn, f"{user_id}@example.com",
                users.UNUSABLE_PASSWORD_HASH, user_id=user_id,
            )
        repository.save(
            conn, user_id,
            source.fetch_accounts(),
            source.fetch_transactions().added,
            connection=source.connection(),
        )
        if version is not None:
            conn.execute(
                "UPDATE meta SET schema_version = ? WHERE id = 1", (version,)
            )
            conn.commit()
    finally:
        conn.close()
    return path


def _v6(tmp_path, name="v6.db", user_id=USER):
    """A database exactly as Step 6.3 left it: connection ids unscoped."""
    return _seed(tmp_path / name, user_id, FakeSource(), version=6)


def _v7(tmp_path, name="v7.db", user_id=USER):
    """The same fixture, synced by the current code."""
    return _seed(tmp_path / name, user_id, FakeSource(
        connection_id=scoped_connection_id(CONNECTION_ID, user_id)
    ))


def _rows(path, sql, *args):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _render(path, monkeypatch):
    buffer = StringIO()
    monkeypatch.setenv("DB_PATH", str(path))
    app.main(console=Console(file=buffer, width=80, legacy_windows=False,
                             no_color=True, highlight=False))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

def test_the_registry_reaches_the_current_schema_version():
    """A bump without a step orphans every existing database, and it does so
    silently until somebody with an old file tries to open it. This is the
    check that turns that into a failing test at the moment of the bump."""
    assert max(migrations.MIGRATIONS) + 1 == store.SCHEMA_VERSION


def test_every_version_from_the_floor_up_has_a_step():
    missing = [
        version
        for version in range(migrations.EARLIEST_SUPPORTED_VERSION,
                             store.SCHEMA_VERSION)
        if version not in migrations.MIGRATIONS
    ]
    assert not missing


def test_a_bump_with_no_step_is_refused_rather_than_skipped(tmp_path,
                                                            monkeypatch):
    """The failure this prevents: `upgrade` shrugging at a gap, stamping the
    new version onto a file that never got the change, and handing back a
    connection whose next write fails on a column that is not there."""
    path = _v6(tmp_path)
    monkeypatch.setattr(migrations, "MIGRATIONS", {})

    with pytest.raises(migrations.MigrationFailed, match="no migration"):
        store.open_db(str(path))


# ---------------------------------------------------------------------------
# v6 -> v7: the rekeying
# ---------------------------------------------------------------------------

def test_a_v6_fixture_connection_is_rekeyed_to_its_owner(tmp_path):
    path = _v6(tmp_path)
    store.open_db(str(path)).close()

    (row,) = _rows(path, "SELECT connection_id, user_id FROM connections")
    assert row["connection_id"] == f"{CONNECTION_ID}:{USER}"
    assert row["user_id"] == USER


def test_the_version_is_stamped_only_after_the_step_ran(tmp_path):
    path = _v6(tmp_path)
    store.open_db(str(path)).close()

    (meta,) = _rows(path, "SELECT schema_version FROM meta WHERE id = 1")
    assert meta["schema_version"] == store.SCHEMA_VERSION


def test_every_account_and_transaction_id_moves_with_it(tmp_path):
    path = _v6(tmp_path)
    before = len(_rows(path, "SELECT id FROM transactions"))
    store.open_db(str(path)).close()

    scoped = f"{CONNECTION_ID}:{USER}".replace(":", "%3A")
    prefix = f"fake:{scoped}:"

    accounts = _rows(path, "SELECT id, connection_id FROM accounts")
    transactions = _rows(path, "SELECT id, connection_id FROM transactions")

    assert accounts and len(transactions) == before
    for row in [*accounts, *transactions]:
        assert row["id"].startswith(prefix), row["id"]
        assert row["connection_id"] == f"{CONNECTION_ID}:{USER}"


def test_every_transaction_still_resolves_to_its_account(tmp_path):
    """THE ONE THAT MATTERS, and the reason the step rewrites `account_id` in
    the same statement as the id. Rekey the accounts and leave the children
    pointing at the old strings and the join in every read returns nothing --
    a dashboard that renders perfectly with no accounts and no money on it."""
    path = _v6(tmp_path)
    store.open_db(str(path)).close()

    orphans = _rows(
        path,
        "SELECT t.id FROM transactions t "
        "LEFT JOIN accounts a ON a.id = t.account_id WHERE a.id IS NULL",
    )
    assert orphans == []


def test_the_money_is_unchanged(tmp_path):
    path = _v6(tmp_path)
    before = _rows(path, "SELECT sum(amount_cents) AS total, count(*) AS n "
                         "FROM transactions")[0]
    store.open_db(str(path)).close()
    after = _rows(path, "SELECT sum(amount_cents) AS total, count(*) AS n "
                        "FROM transactions")[0]

    assert (after["total"], after["n"]) == (before["total"], before["n"])


def test_a_migrated_database_is_indistinguishable_from_a_fresh_sync(tmp_path):
    """The strongest statement available: the migration lands on exactly the
    ids the current sync path would have written, rather than somewhere merely
    self-consistent. If these drift, the next sync inserts a second copy of
    every row beside the migrated one."""
    migrated = _v6(tmp_path)
    fresh = _v7(tmp_path)
    store.open_db(str(migrated)).close()

    for table in ("accounts", "transactions"):
        assert (
            [r["id"] for r in _rows(migrated, f"SELECT id FROM {table} ORDER BY id")]
            == [r["id"] for r in _rows(fresh, f"SELECT id FROM {table} ORDER BY id")]
        )


def test_the_dashboard_renders_identically_either_way(tmp_path, monkeypatch):
    """The render diff this step was predicted to produce, asserted as absent.

    Nothing on screen is an id, and the one place ordering could have moved --
    `ORDER BY t.date, t.id` under a stable sort in main.py -- is safe because
    every id in a connection gains the SAME new prefix, so their relative order
    cannot change. The baseline gate agrees; this says it against a database
    that was actually migrated rather than seeded."""
    migrated = _v6(tmp_path)
    fresh = _v7(tmp_path)
    store.open_db(str(migrated)).close()

    output = _render(migrated, monkeypatch)

    assert "Net Worth" in output, "an empty screen would match trivially"
    assert output == _render(fresh, monkeypatch)


def test_migrating_twice_is_a_no_op(tmp_path):
    path = _v6(tmp_path)
    store.open_db(str(path)).close()
    first = [r["id"] for r in _rows(path, "SELECT id FROM accounts ORDER BY id")]

    store.open_db(str(path)).close()
    second = [r["id"] for r in _rows(path, "SELECT id FROM accounts ORDER BY id")]

    assert first == second


def test_an_already_scoped_connection_is_left_alone(tmp_path):
    """Idempotence at the step level, not just at the version check. A file
    stamped v6 by hand but already holding scoped ids must not gain a second
    user segment."""
    path = _v7(tmp_path, name="scoped.db")
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE meta SET schema_version = 6 WHERE id = 1")
    conn.commit()
    conn.close()

    store.open_db(str(path)).close()

    (row,) = _rows(path, "SELECT connection_id FROM connections")
    assert row["connection_id"] == f"{CONNECTION_ID}:{USER}"


def test_a_plaid_connection_is_not_rekeyed(tmp_path):
    """An Item id is already unique per user per institution. Scoping it would
    rekey every linked account to buy a property it has by construction -- on
    the one connection a re-sync cannot rebuild."""
    path = _v6(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO connections (provider, connection_id, user_id) "
        "VALUES ('plaid', 'item_9', ?)", (USER,),
    )
    conn.commit()
    conn.close()

    store.open_db(str(path)).close()

    (row,) = _rows(path, "SELECT connection_id FROM connections "
                         "WHERE provider = 'plaid'")
    assert row["connection_id"] == "item_9"


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

def test_a_step_that_raises_leaves_the_database_untouched(tmp_path,
                                                          monkeypatch):
    """Foreign keys are off while a step runs, so a partial rewrite is a file
    with accounts under one connection id and transactions under another --
    which reads as an empty dashboard. The transaction is what makes the
    outcome all-or-nothing."""
    path = _v6(tmp_path)
    before = _rows(path, "SELECT id, connection_id FROM accounts ORDER BY id")

    def half_way(conn):
        migrations.to_v7(conn)
        raise RuntimeError("interrupted")

    monkeypatch.setattr(migrations, "MIGRATIONS", {6: half_way, 7: migrations.to_v8})

    with pytest.raises(RuntimeError, match="interrupted"):
        store.open_db(str(path))

    after = _rows(path, "SELECT id, connection_id FROM accounts ORDER BY id")
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
    assert _rows(path, "SELECT schema_version FROM meta")[0][0] == 6


def test_a_step_that_orphans_a_row_is_rolled_back(tmp_path, monkeypatch):
    """The check that has to exist because the FKs are off: a step that rewrites
    the accounts and forgets the transactions would otherwise commit happily and
    take the whole dashboard with it."""
    path = _v6(tmp_path)

    def accounts_only(conn):
        conn.execute(
            "UPDATE accounts SET id = id || '-moved' WHERE provider = 'fake'"
        )

    monkeypatch.setattr(migrations, "MIGRATIONS", {6: accounts_only, 7: migrations.to_v8})

    with pytest.raises(migrations.MigrationFailed, match="parent that does not"):
        store.open_db(str(path))

    assert _rows(path, "SELECT count(*) FROM accounts WHERE id LIKE '%-moved'")[0][0] == 0


def test_foreign_keys_are_enforced_again_after_a_migration(tmp_path):
    """`PRAGMA foreign_keys` is per connection and the step turns it OFF. If it
    stayed off, the connection handed back would accept an orphaned write for
    the rest of its life -- and it is the same connection sync.py then uses."""
    path = _v6(tmp_path)
    conn = store.open_db(str(path))
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO accounts "
                "(id, provider, provider_id, connection_id, name, institution, "
                " type, balance_cents) "
                "VALUES ('x', 'fake', 'x', 'nonexistent', 'n', 'i', "
                "        'checking', 0)"
            )
    finally:
        conn.close()


def test_a_rekey_onto_an_existing_connection_is_refused(tmp_path):
    """Two rows would have to merge, and an upsert picking a winner is the
    silent overwrite the scoping exists to prevent."""
    path = _v6(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO connections (provider, connection_id, user_id) "
        "VALUES ('fake', ?, ?)", (f"{CONNECTION_ID}:{USER}", USER),
    )
    conn.commit()
    conn.close()

    with pytest.raises(migrations.MigrationFailed, match="already exists"):
        store.open_db(str(path))


def test_a_new_database_is_never_migrated(tmp_path):
    """A fresh file is stamped at SCHEMA_VERSION by init_schema, so running a
    step against it would be running v6 -> v7 logic over v7 data."""
    conn = store.connect(str(tmp_path / "new.db"))
    try:
        assert store.init_schema(conn) == []
    finally:
        conn.close()


def test_an_up_to_date_database_reports_no_migrations(tmp_path):
    path = _v7(tmp_path)
    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == []
    finally:
        conn.close()


def test_a_migrated_database_reports_which_versions_it_passed(tmp_path):
    path = _v6(tmp_path)
    conn = store.connect(str(path))
    try:
        assert store.init_schema(conn) == [7, 8]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The floor
# ---------------------------------------------------------------------------

def test_the_floor_is_v6_and_says_why(tmp_path):
    path = _v6(tmp_path, name="ancient.db")
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE meta SET schema_version = 5 WHERE id = 1")
    conn.commit()
    conn.close()

    with pytest.raises(migrations.UnsupportedSchemaVersion) as caught:
        store.open_db(str(path))

    assert migrations.EARLIEST_SUPPORTED_VERSION == 6
    assert "delete it and re-sync" in str(caught.value).lower()


def test_a_newer_database_is_still_refused_outright(tmp_path):
    """Unchanged by the arrival of migrations, and deliberately so: there is no
    migrating backwards, and the extra columns are invisible to every SELECT we
    write."""
    path = _v7(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE meta SET schema_version = ? WHERE id = 1",
                 (store.SCHEMA_VERSION + 1,))
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="NEWER than"):
        store.open_db(str(path))
