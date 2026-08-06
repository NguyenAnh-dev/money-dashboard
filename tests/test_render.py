"""
tests/test_render.py
--------------------
The rendering decisions Step 5.6 introduced, none of which the baseline gate
can see.

The gate renders exactly one situation: a populated database synced from the
fixture. Everything new at 5.6 is about the situations OTHER than that one --
an empty database, a missing file, a header that must contradict the
environment, a footer that must report stored state. A passing gate says
nothing about any of them.
"""

from __future__ import annotations

from datetime import date, datetime
from io import StringIO

import pytest
from rich.console import Console

import main as app
from adapters.fake import FakeSource
from core import repository, store, users
from core.models import (
    Account,
    AccountType,
    ConnectionRef,
    Provider,
    SourceRef,
    SyncResult,
    make_id,
)


def _console(buffer):
    """The gate's pins, minus DB_PATH which each test sets for itself."""
    return Console(
        file=buffer, width=80, legacy_windows=False, no_color=True,
        highlight=False,
    )


def _render(db_path, monkeypatch) -> str:
    monkeypatch.setenv("DB_PATH", str(db_path))
    buffer = StringIO()
    app.main(console=_console(buffer))
    return buffer.getvalue()


def _seed(db_path, accounts, transactions, synced_at=None):
    conn = store.open_db(str(db_path))
    try:
        repository.save(conn, users.ensure_local_user(conn),
                        accounts, transactions,
                        synced_at=synced_at or datetime(2026, 8, 1, 9, 0))
    finally:
        conn.close()


def _account(provider, provider_id="acc_1", institution="Chase") -> Account:
    ref = SourceRef(
        provider=provider, provider_id=provider_id, connection_id="c",
    )
    return Account(
        id=make_id(ref),
        source=ref,
        name="Checking",
        institution=institution,
        type=AccountType.CHECKING,
        balance_cents=100_000,
    )


# ---------------------------------------------------------------------------
# The empty cases
# ---------------------------------------------------------------------------

def test_a_missing_database_shows_guidance_instead_of_empty_tables(tmp_path, monkeypatch):
    """The failure mode 5.1 accepted, and the first thing a new user hits.
    Zero rows would draw every table empty at $0.00, which reads as a broken
    dashboard rather than an unsynced one."""
    output = _render(tmp_path / "absent.db", monkeypatch)

    assert "No data yet" in output
    assert "python sync.py" in output
    assert "Net Worth" not in output
    assert "Recent Transactions" not in output


def test_rendering_never_creates_the_database(tmp_path, monkeypatch):
    """store.open_db() would CREATE the file, and creating a database as a side
    effect of a read-only command leaves an empty money.db in whatever
    directory someone ran this from -- which then renders 'no data' forever
    while the real database sits elsewhere."""
    missing = tmp_path / "absent.db"
    _render(missing, monkeypatch)
    assert not missing.exists()


def test_an_existing_but_empty_database_gets_the_same_screen(tmp_path, monkeypatch):
    """A file with no rows is the same situation as no file: a sync that has
    not happened, or one that failed and rolled back."""
    path = tmp_path / "empty.db"
    store.open_db(str(path)).close()

    output = _render(path, monkeypatch)
    assert "No data yet" in output
    assert "Net Worth" not in output


def test_the_empty_screen_names_the_path_it_looked_at(tmp_path, monkeypatch):
    """A misdirected DB_PATH produces exactly the same empty screen as never
    having synced. The path is the only thing that distinguishes them."""
    path = tmp_path / "absent.db"
    output = _render(path, monkeypatch)
    assert "absent.db" in output.replace("\n", "")


# ---------------------------------------------------------------------------
# The header: provenance from the rows
# ---------------------------------------------------------------------------

def test_the_header_reads_the_provider_from_the_rows(tmp_path, monkeypatch):
    path = tmp_path / "fake.db"
    source = FakeSource()
    _seed(path, source.fetch_accounts(), source.fetch_transactions().added)

    assert "v0.1 - fake data" in _render(path, monkeypatch)


def test_the_header_ignores_data_source(tmp_path, monkeypatch):
    """THE 5.6 test. DATA_SOURCE says what the NEXT sync would fetch; the screen
    shows what the LAST sync stored. Before this, `DATA_SOURCE=plaid python
    main.py` against a fixture-synced database labelled fake data 'plaid' --
    the same lie the hardcoded header told at 4.7, through a door that only
    opened when sync and render split."""
    path = tmp_path / "fake.db"
    source = FakeSource()
    _seed(path, source.fetch_accounts(), source.fetch_transactions().added)

    monkeypatch.setenv("DATA_SOURCE", "plaid")
    output = _render(path, monkeypatch)

    assert "v0.1 - fake data" in output
    assert "plaid" not in output


def test_a_mixed_provider_database_names_every_provider(tmp_path, monkeypatch):
    """One database can legitimately hold two sources. Naming whichever synced
    last would mislabel the rest, so the label joins them."""
    path = tmp_path / "mixed.db"
    _seed(path, [
        _account(Provider.FAKE, "acc_1", "Chase"),
        _account(Provider.CSV, "acc_2", "Imported"),
    ], [])

    assert "v0.1 - csv, fake data" in _render(path, monkeypatch)


def test_source_label_joins_providers():
    assert app.source_label(["fake"]) == "fake"
    assert app.source_label(["csv", "fake"]) == "csv, fake"


# ---------------------------------------------------------------------------
# The footer: staleness
# ---------------------------------------------------------------------------

def test_the_footer_reports_the_stored_sync_time(tmp_path, monkeypatch):
    """meta.last_synced_at's whole purpose. With sync and render split,
    week-old data renders exactly as confidently as fresh data, and this line
    is the only thing on screen that says which one you are looking at."""
    path = tmp_path / "fake.db"
    source = FakeSource()
    _seed(path, source.fetch_accounts(), source.fetch_transactions().added,
          synced_at=datetime(2026, 7, 4, 18, 45, 30))

    assert "last synced 2026-07-04 18:45" in _render(path, monkeypatch)


def test_format_synced_at_drops_the_t_and_the_seconds():
    assert app.format_synced_at("2026-08-01T09:00:00") == "2026-08-01 09:00"


def test_format_synced_at_handles_never():
    assert app.format_synced_at(None) == "never"


def test_the_footer_is_absolute_never_relative(tmp_path, monkeypatch):
    """'3 days ago' is friendlier and cannot be baselined: the stored timestamp
    is fixed while `now` moves, so the gate would pass on the day it was
    recorded and fail every day after. A gate that expires is worse than a
    slightly less friendly footer."""
    path = tmp_path / "fake.db"
    source = FakeSource()
    _seed(path, source.fetch_accounts(), source.fetch_transactions().added,
          synced_at=datetime(2020, 1, 1, 0, 0))

    output = _render(path, monkeypatch)
    assert "last synced 2020-01-01 00:00" in output
    for phrase in ("ago", "days", "today", "yesterday"):
        assert phrase not in output.lower()


# ---------------------------------------------------------------------------
# The render is a pure function of the database
# ---------------------------------------------------------------------------

def test_the_same_database_renders_identically_twice(tmp_path, monkeypatch):
    """No wall clock, no environment, no network anywhere in the read path."""
    path = tmp_path / "fake.db"
    source = FakeSource()
    _seed(path, source.fetch_accounts(), source.fetch_transactions().added)

    assert _render(path, monkeypatch) == _render(path, monkeypatch)


# ---------------------------------------------------------------------------
# sync.py: one source instance, fetched from twice
# ---------------------------------------------------------------------------

def test_sync_builds_exactly_one_source(tmp_path, monkeypatch):
    """Before 5.7 this was two: get_accounts() and get_transactions() each call
    data_source.get_source(). For a CSV that is two file reads; for Plaid two
    SDK clients and a second /item/get, with a window between the fetches in
    which a new account can appear and orphan a transaction."""
    import data_source
    import sync as sync_module

    built = []
    real = data_source.get_source

    def counting_get_source():
        built.append(1)
        return real()

    monkeypatch.setenv("DB_PATH", str(tmp_path / "sync.db"))
    monkeypatch.setattr(sync_module, "get_source", counting_get_source)
    sync_module.sync()

    assert len(built) == 1


def test_sync_fetches_transactions_before_accounts(tmp_path, monkeypatch):
    """The order closes the orphan window rather than merely narrowing it.
    Accounts fetched SECOND are a snapshot taken later, so any account a
    transaction already referenced is in it. Fetching accounts first leaves the
    gap: an account opened between the two calls arrives with transactions
    pointing at nothing, and the foreign key rejects the whole sync."""
    import sync as sync_module

    calls = []

    class Recording:
        def connection(self):
            return ConnectionRef(provider=Provider.FAKE, connection_id="rec")

        def fetch_accounts(self):
            calls.append("accounts")
            return []

        def fetch_transactions(self, cursor=None):
            calls.append("transactions")
            return SyncResult()

    monkeypatch.setenv("DB_PATH", str(tmp_path / "sync.db"))
    monkeypatch.setattr(sync_module, "get_source", lambda: Recording())
    sync_module.sync()

    assert calls == ["transactions", "accounts"]


def test_sync_writes_what_the_source_returned(tmp_path, monkeypatch):
    import sync as sync_module

    monkeypatch.setenv("DB_PATH", str(tmp_path / "sync.db"))
    monkeypatch.setenv("DATA_SOURCE", "fake")
    runs = sync_module.sync()

    source = FakeSource()
    assert len(runs) == 1, "one configured source, no linked connections"
    assert runs[0].connection.connection_id == "fixture"
    assert runs[0].result.accounts == len(source.fetch_accounts())
    assert runs[0].result.transactions == len(source.fetch_transactions().added)


def test_main_imports_neither_data_source_nor_any_adapter():
    """5.1's split, asserted structurally. Rendering must not be able to reach
    an adapter, a provider SDK, or the network.

    Parsed as an AST rather than searched as text: main.py's comments discuss
    data_source at length, and a substring check matches the prose explaining
    why the import is gone. Only real import statements count."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(app))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "data_source" not in imported
    assert not any(name.startswith("adapters") for name in imported)
    assert "plaid" not in imported
