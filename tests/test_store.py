"""
tests/test_store.py
-------------------
The invariants Step 5.3 verified BY HAND, written down so they survive.

Hand-verification is evidence that evaporates: nobody re-runs it, and the day
one of these regresses the symptom is a silently wrong dashboard rather than a
crash. Everything here was checked once in a terminal at 5.3 -- foreign keys
enforced, the typeof() guard biting, the version refusal in both directions,
an overpaid credit card accepted -- plus the 5.4 write path on top.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime

import pytest

from adapters.fake import FakeSource
from core import repository, store
from core.models import (
    Account,
    AccountType,
    Provider,
    SourceRef,
    Transaction,
    TransactionKind,
    make_id,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """A fresh database per test, in pytest's temp dir -- never DB_PATH.

    A test that touched the real money.db would be a test that destroys the
    user's data on a bad day.
    """
    conn = store.open_db(str(tmp_path / "test.db"))
    yield conn
    conn.close()


def _account(provider_id="acc_1", balance_cents=100_000, **kwargs) -> Account:
    ref = SourceRef(
        provider=Provider.FAKE,
        provider_id=provider_id,
        connection_id="fixture",
    )
    fields = dict(
        id=make_id(ref),
        source=ref,
        name="Chase Checking",
        institution="Chase",
        type=AccountType.CHECKING,
        balance_cents=balance_cents,
    )
    fields.update(kwargs)
    return Account(**fields)


def _transaction(provider_id="txn_1", account=None, **kwargs) -> Transaction:
    account = account or _account()
    ref = SourceRef(
        provider=Provider.FAKE,
        provider_id=provider_id,
        connection_id="fixture",
    )
    fields = dict(
        id=make_id(ref),
        source=ref,
        account_id=account.id,
        date=date(2026, 7, 27),
        name="SQ *BLUE BOTTLE",
        merchant="Blue Bottle",
        amount_cents=575,
        category="Food & Drink",
    )
    fields.update(kwargs)
    return Transaction(**fields)


# ---------------------------------------------------------------------------
# 5.3: the schema itself
# ---------------------------------------------------------------------------

def test_open_db_creates_both_tables_and_the_meta_row(db):
    tables = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )}
    assert {"accounts", "transactions", "meta"} <= tables

    meta = db.execute("SELECT * FROM meta").fetchall()
    assert len(meta) == 1
    assert meta[0]["schema_version"] == store.SCHEMA_VERSION
    assert meta[0]["last_synced_at"] is None
    assert meta[0]["sync_cursor"] is None


def test_opening_an_existing_database_is_idempotent(tmp_path):
    path = str(tmp_path / "twice.db")
    first = store.open_db(path)
    repository.save(first, [_account()], [])
    first.close()

    second = store.open_db(path)
    assert second.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 1
    assert second.execute("SELECT COUNT(*) c FROM meta").fetchone()["c"] == 1
    second.close()


def test_foreign_keys_are_enforced(db):
    """PRAGMA foreign_keys is per-CONNECTION, so this is really a test of
    store.connect() remembering to set it. Without it the FK is decoration and
    the one invariant STRUCTURE exists to catch goes unenforced while looking
    enforced."""
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    with pytest.raises(sqlite3.IntegrityError):
        repository.save_transactions(db, [_transaction()])


def test_an_integer_column_rejects_a_float(db):
    """INTEGER is an AFFINITY, not a type: without the typeof() CHECK, 12.34
    stores as a REAL and invariant 1 is breached in the layer everyone assumes
    is typed. __post_init__ cannot help -- raw SQL is a second door in."""
    with pytest.raises(sqlite3.IntegrityError, match="typeof"):
        db.execute(
            "INSERT INTO accounts "
            "(id, provider, provider_id, connection_id, name, institution, "
            " type, balance_cents) "
            "VALUES ('a', 'fake', 'a', 'c', 'n', 'i', 'checking', 12.34)"
        )


def test_a_float_amount_is_rejected_too(db):
    repository.save_accounts(db, [_account()])
    with pytest.raises(sqlite3.IntegrityError, match="typeof"):
        db.execute(
            "INSERT INTO transactions "
            "(id, provider, provider_id, connection_id, account_id, date, "
            " name, merchant, amount_cents, category) "
            "VALUES ('t', 'fake', 't', 'fixture', ?, '2026-07-27', 'n', 'm', "
            "        5.75, 'c')",
            (_account().id,),
        )


def test_an_overpaid_credit_card_is_accepted(db):
    """The 5.2 correction, pinned. A card with a credit balance -- you overpaid,
    or a refund landed early -- genuinely has money owed TO you, and a CHECK
    rejecting it would abort a sync on legitimate data. Catching an inverted
    sign flip is conformance's job, not the schema's."""
    overpaid = _account(
        provider_id="amex", type=AccountType.CREDIT, balance_cents=5_000
    )
    repository.save_accounts(db, [overpaid])
    db.commit()

    stored = db.execute(
        "SELECT balance_cents FROM accounts WHERE id = ?", (overpaid.id,)
    ).fetchone()
    assert stored["balance_cents"] == 5_000


def test_a_datetime_cannot_be_stored_in_the_date_column(db):
    repository.save_accounts(db, [_account()])
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO transactions "
            "(id, provider, provider_id, connection_id, account_id, date, "
            " name, merchant, amount_cents, category) "
            "VALUES ('t', 'fake', 't', 'fixture', ?, '2026-07-27T00:00:00', "
            "        'n', 'm', 575, 'c')",
            (_account().id,),
        )


def test_an_empty_merchant_is_rejected(db):
    repository.save_accounts(db, [_account()])
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO transactions "
            "(id, provider, provider_id, connection_id, account_id, date, "
            " name, merchant, amount_cents, category) "
            "VALUES ('t', 'fake', 't', 'fixture', ?, '2026-07-27', 'n', '', "
            "        575, 'c')",
            (_account().id,),
        )


# ---------------------------------------------------------------------------
# 5.3: the version gate
# ---------------------------------------------------------------------------

def _force_version(path: str, version: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute("UPDATE meta SET schema_version = ?", (version,))
    conn.commit()
    conn.close()


def test_an_older_schema_version_is_refused(tmp_path):
    path = str(tmp_path / "old.db")
    store.open_db(path).close()
    _force_version(path, store.SCHEMA_VERSION - 1)

    with pytest.raises(RuntimeError, match="older than"):
        store.open_db(path)


def test_a_newer_schema_version_is_refused(tmp_path):
    """The counterintuitive half, and the one worth keeping. A newer file reads
    fine -- its extra columns are invisible to every SELECT we write -- so the
    app would render plausible numbers and then drop those columns on the next
    write. Refusing is what keeps someone else's data alive."""
    path = str(tmp_path / "new.db")
    store.open_db(path).close()
    _force_version(path, store.SCHEMA_VERSION + 1)

    with pytest.raises(RuntimeError, match="NEWER than"):
        store.open_db(path)


def test_a_missing_meta_row_is_refused(tmp_path):
    path = str(tmp_path / "nometa.db")
    store.open_db(path).close()

    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM meta")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="no row 1"):
        store.open_db(path)


# ---------------------------------------------------------------------------
# 5.3: where the file lives
# ---------------------------------------------------------------------------

def test_db_path_defaults_to_the_project_root(monkeypatch):
    """Anchored to core/store.py, NOT to the working directory. A wrong
    relative path to a CSV raises 'file not found'; a wrong relative path to a
    database CREATES an empty one wherever you were standing, and the dashboard
    renders zero accounts as though you had never synced."""
    monkeypatch.delenv("DB_PATH", raising=False)
    resolved = store.db_path()

    assert resolved.endswith("money.db")
    assert resolved == str(store.PROJECT_ROOT / store.DEFAULT_DB_NAME)


def test_db_path_honours_the_env_var(monkeypatch):
    monkeypatch.setenv("DB_PATH", "/tmp/elsewhere.db")
    assert store.db_path() == "/tmp/elsewhere.db"


def test_db_path_is_read_per_call_not_at_import(monkeypatch):
    monkeypatch.setenv("DB_PATH", "/tmp/first.db")
    assert store.db_path() == "/tmp/first.db"
    monkeypatch.setenv("DB_PATH", "/tmp/second.db")
    assert store.db_path() == "/tmp/second.db"


# ---------------------------------------------------------------------------
# 5.4: the write path
# ---------------------------------------------------------------------------

def test_save_writes_accounts_and_transactions(db):
    account = _account()
    result = repository.save(db, [account], [_transaction(account=account)])

    assert result.accounts == 1
    assert result.transactions == 1
    assert db.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 1
    assert db.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"] == 1


def test_every_model_field_survives_the_write(db):
    account = _account(
        mask="4321", available_cents=99_000, as_of=date(2026, 7, 27)
    )
    txn = _transaction(
        account=account,
        kind=TransactionKind.TRANSFER,
        pending=True,
        currency="usd",
        raw={"amount": 5.75, "authorized_date": date(2026, 7, 26)},
    )
    repository.save(db, [account], [txn])

    a = db.execute("SELECT * FROM accounts").fetchone()
    assert a["id"] == account.id
    assert a["provider"] == "fake"
    assert a["provider_id"] == "acc_1"
    assert a["connection_id"] == "fixture"
    assert a["fetched_at"] is None
    assert a["type"] == "checking"
    assert a["mask"] == "4321"
    assert a["available_cents"] == 99_000
    assert a["as_of"] == "2026-07-27"

    t = db.execute("SELECT * FROM transactions").fetchone()
    assert t["account_id"] == account.id
    assert t["date"] == "2026-07-27"
    assert t["name"] == "SQ *BLUE BOTTLE"
    assert t["merchant"] == "Blue Bottle"
    assert t["amount_cents"] == 575
    assert t["currency"] == "USD"
    assert t["kind"] == "transfer"
    assert t["pending"] == 1


def test_enums_are_stored_as_their_string_value(db):
    account = _account(type=AccountType.CREDIT, balance_cents=-1_000)
    repository.save(db, [account], [
        _transaction(account=account, kind=TransactionKind.INCOME)
    ])

    assert db.execute("SELECT type FROM accounts").fetchone()["type"] == "credit"
    assert db.execute("SELECT kind FROM transactions").fetchone()["kind"] == "income"


def test_fetched_at_is_stored_as_an_iso_datetime(db):
    fetched = datetime(2026, 7, 27, 14, 3, 11)
    ref = SourceRef(
        provider=Provider.FAKE,
        provider_id="acc_1",
        connection_id="fixture",
        fetched_at=fetched,
    )
    account = _account(source=ref, id=make_id(ref))
    repository.save(db, [account], [])

    stored = db.execute("SELECT fetched_at FROM accounts").fetchone()
    assert stored["fetched_at"] == "2026-07-27T14:03:11"


# ---------------------------------------------------------------------------
# 5.4: upsert
# ---------------------------------------------------------------------------

def test_a_resync_updates_instead_of_duplicating(db):
    account = _account()
    txns = [_transaction(account=account)]

    repository.save(db, [account], txns)
    repository.save(db, [account], txns)

    assert db.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 1
    assert db.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"] == 1


def test_a_changed_balance_overwrites_the_stored_one(db):
    repository.save(db, [_account(balance_cents=100_000)], [])
    repository.save(db, [_account(balance_cents=250_000)], [])

    stored = db.execute("SELECT balance_cents FROM accounts").fetchone()
    assert stored["balance_cents"] == 250_000


def test_a_pending_charge_that_settles_updates_the_same_row(db):
    """The id deliberately excludes `pending`, so a row that only flips its flag
    keeps its id and the upsert updates it in place. The unsolved half -- an
    amount or date that MOVES on settling, which rehashes to a new id -- is
    still Step 5 work; this pins the half that does work."""
    account = _account()
    repository.save(db, [account], [_transaction(account=account, pending=True)])
    repository.save(db, [account], [_transaction(account=account, pending=False)])

    rows = db.execute("SELECT pending FROM transactions").fetchall()
    assert len(rows) == 1
    assert rows[0]["pending"] == 0


def test_an_upsert_refreshes_every_column_not_just_the_changed_one(db):
    account = _account()
    repository.save(db, [account], [_transaction(account=account)])
    repository.save(db, [account], [
        _transaction(
            account=account,
            merchant="Blue Bottle Coffee",
            category="Coffee",
            amount_cents=625,
        )
    ])

    t = db.execute("SELECT * FROM transactions").fetchone()
    assert t["merchant"] == "Blue Bottle Coffee"
    assert t["category"] == "Coffee"
    assert t["amount_cents"] == 625


# ---------------------------------------------------------------------------
# 5.4: atomicity and ordering
# ---------------------------------------------------------------------------

def test_a_failed_transaction_write_rolls_back_the_accounts(db):
    """Why save() is one transaction. A half-written sync is worse than a
    failed one: the accounts panel would show new balances while the spending
    tables still described last week."""
    orphan = _transaction(account=_account(provider_id="does_not_exist"))

    with pytest.raises(sqlite3.IntegrityError):
        repository.save(db, [_account()], [orphan])

    assert db.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"] == 0


def test_a_failed_write_leaves_last_synced_at_untouched(db):
    repository.save(db, [_account()], [], synced_at=datetime(2026, 7, 1, 9, 0))
    orphan = _transaction(account=_account(provider_id="does_not_exist"))

    with pytest.raises(sqlite3.IntegrityError):
        repository.save(db, [_account()], [orphan],
                        synced_at=datetime(2026, 7, 28, 9, 0))

    assert repository.last_synced_at(db) == "2026-07-01T09:00:00"


def test_accounts_must_be_written_before_transactions(db):
    """The ordering inside save() is load-bearing, not stylistic."""
    account = _account()
    with pytest.raises(sqlite3.IntegrityError):
        repository.save_transactions(db, [_transaction(account=account)])
        db.commit()


# ---------------------------------------------------------------------------
# 5.4: raw
# ---------------------------------------------------------------------------

def test_raw_defaults_to_an_empty_json_object(db):
    account = _account()
    repository.save(db, [account], [_transaction(account=account)])

    stored = db.execute("SELECT raw FROM transactions").fetchone()["raw"]
    assert stored == "{}"
    assert json.loads(stored) == {}


def test_raw_serializes_the_date_objects_json_refuses(db):
    """The serializer flagged back at 5.2. Live Plaid payloads carry date
    objects, and json.dumps raises TypeError on them -- so without the encoder
    every real Plaid sync dies on the first row."""
    account = _account()
    txn = _transaction(
        account=account,
        raw={"date": date(2026, 7, 27), "authorized": datetime(2026, 7, 26, 8, 30)},
    )
    repository.save(db, [account], [txn])

    stored = json.loads(db.execute("SELECT raw FROM transactions").fetchone()["raw"])
    assert stored == {"date": "2026-07-27", "authorized": "2026-07-26T08:30:00"}


def test_raw_is_lossy_on_types_by_design(db):
    """Stated so nobody writes a round-trip equality test and is surprised. A
    date goes out as a string and comes back a string; `raw` is an archive to
    read fields out of, not a dict to reconstruct."""
    original = {"date": date(2026, 7, 27)}
    restored = json.loads(repository.encode_raw(original))

    assert restored != original
    assert restored == {"date": "2026-07-27"}


def test_raw_encoding_is_deterministic(db):
    """Key order must not decide whether a re-sync counts as a change."""
    first = repository.encode_raw({"b": 1, "a": 2})
    second = repository.encode_raw({"a": 2, "b": 1})
    assert first == second


def test_a_field_json_genuinely_cannot_encode_still_raises(db):
    with pytest.raises(TypeError):
        repository.encode_raw({"nope": object()})


# ---------------------------------------------------------------------------
# 5.4: the sync stamp
# ---------------------------------------------------------------------------

def test_save_stamps_last_synced_at(db):
    assert repository.last_synced_at(db) is None

    result = repository.save(db, [_account()], [],
                             synced_at=datetime(2026, 7, 28, 11, 30, 15))

    assert result.synced_at == "2026-07-28T11:30:15"
    assert repository.last_synced_at(db) == "2026-07-28T11:30:15"


def test_a_cursor_is_stored_when_the_source_supplies_one(db):
    repository.save(db, [_account()], [], cursor="cursor-abc")
    assert repository.sync_cursor(db) == "cursor-abc"


def test_a_source_without_a_cursor_does_not_erase_the_stored_one(db):
    """The reason `save` skips the UPDATE on None rather than writing NULL.

    One meta row holds one cursor, so a `DATA_SOURCE=fake` sync into a database
    that also holds Plaid rows would otherwise wipe Plaid's resume token -- and
    the damage is invisible until the next Plaid sync silently refetches from
    the beginning of time. The real fix is a per-connection cursor at 5.8; this
    keeps the scaffolding from destroying data in the meantime.
    """
    repository.save(db, [_account()], [], cursor="cursor-abc")
    repository.save(db, [_account()], [], cursor=None)

    assert repository.sync_cursor(db) == "cursor-abc"


def test_a_new_database_has_no_cursor(db):
    assert repository.sync_cursor(db) is None


def test_save_defaults_the_stamp_to_now(db):
    before = datetime.now().isoformat(timespec="seconds")
    repository.save(db, [_account()], [])
    assert repository.last_synced_at(db) >= before


# ---------------------------------------------------------------------------
# 5.4: the real fixture, end to end
# ---------------------------------------------------------------------------

def test_the_whole_fake_fixture_writes_and_re_writes(db):
    """The one test that exercises real adapter output rather than hand-built
    objects -- including the fixture's deliberate traps: the pending Starbucks,
    the Amex transfer, the per-order Amazon descriptors."""
    source = FakeSource()
    accounts = source.fetch_accounts()
    transactions = source.fetch_transactions().added

    first = repository.save(db, accounts, transactions)
    assert first.accounts == len(accounts)
    assert first.transactions == len(transactions)

    repository.save(db, accounts, transactions)

    stored_accounts = db.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]
    stored_txns = db.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"]
    assert stored_accounts == len(accounts)
    assert stored_txns == len(transactions)


# ---------------------------------------------------------------------------
# 5.5: the read path
# ---------------------------------------------------------------------------

def test_the_whole_fixture_round_trips(db):
    """THE test for 5.5. Store the fixture, read it back, assert it matches --
    field by field, not by count. Everything below this narrows in on one
    conversion; this is the one that would catch a conversion nobody thought
    to name."""
    source = FakeSource()
    accounts = source.fetch_accounts()
    transactions = source.fetch_transactions().added
    repository.save(db, accounts, transactions)

    loaded_accounts = repository.load_accounts(db)
    loaded_txns = repository.load_transactions(db)

    assert len(loaded_accounts) == len(accounts)
    assert len(loaded_txns) == len(transactions)

    by_id = {a.id: a for a in loaded_accounts}
    for original in accounts:
        assert by_id[original.id] == original

    by_id = {t.id: t for t in loaded_txns}
    for original in transactions:
        restored = by_id[original.id]
        for field in (
            "id", "account_id", "date", "name", "merchant", "amount_cents",
            "category", "currency", "kind", "pending", "source",
        ):
            assert getattr(restored, field) == getattr(original, field), field


def test_a_date_comes_back_as_a_date_not_a_string(db):
    account = _account()
    repository.save(db, [account], [_transaction(account=account)])

    restored = repository.load_transactions(db)[0]
    assert restored.date == date(2026, 7, 27)
    assert isinstance(restored.date, date)
    assert not isinstance(restored.date, datetime)


def test_pending_comes_back_as_a_real_bool(db):
    """Stored as 0/1, and the model does not guard the field -- so without the
    cast an int flows all the way through. `1 == True` in Python, which is
    exactly why this is easy to miss and worth pinning: `is True` fails, and so
    does anything doing an identity or type check downstream."""
    account = _account()
    repository.save(db, [account], [
        _transaction(provider_id="p", account=account, pending=True),
        _transaction(provider_id="s", account=account, pending=False),
    ])

    by_id = {t.source.provider_id: t for t in repository.load_transactions(db)}
    assert by_id["p"].pending is True
    assert by_id["s"].pending is False


def test_enums_come_back_as_enum_members(db):
    account = _account(type=AccountType.CREDIT, balance_cents=-1_000)
    repository.save(db, [account], [
        _transaction(account=account, kind=TransactionKind.TRANSFER)
    ])

    assert repository.load_accounts(db)[0].type is AccountType.CREDIT
    restored = repository.load_transactions(db)[0]
    assert restored.kind is TransactionKind.TRANSFER
    assert restored.source.provider is Provider.FAKE


def test_source_ref_is_reassembled_from_its_four_columns(db):
    fetched = datetime(2026, 7, 27, 14, 3, 11)
    ref = SourceRef(
        provider=Provider.FAKE,
        provider_id="acc_1",
        connection_id="fixture",
        fetched_at=fetched,
    )
    repository.save(db, [_account(source=ref, id=make_id(ref))], [])

    restored = repository.load_accounts(db)[0].source
    assert restored == ref
    assert restored.fetched_at == fetched


def test_a_null_fetched_at_comes_back_as_none_not_a_string(db):
    repository.save(db, [_account()], [])
    assert repository.load_accounts(db)[0].source.fetched_at is None


def test_nullable_account_fields_survive_being_absent(db):
    repository.save(db, [_account()], [])

    restored = repository.load_accounts(db)[0]
    assert restored.mask is None
    assert restored.available_cents is None
    assert restored.as_of is None


def test_nullable_account_fields_survive_being_present(db):
    repository.save(db, [_account(
        mask="4321", available_cents=99_000, as_of=date(2026, 7, 27)
    )], [])

    restored = repository.load_accounts(db)[0]
    assert restored.mask == "4321"
    assert restored.available_cents == 99_000
    assert restored.as_of == date(2026, 7, 27)


def test_raw_comes_back_as_a_dict(db):
    account = _account()
    repository.save(db, [account], [
        _transaction(account=account, raw={"pending_transaction_id": "abc123"})
    ])

    restored = repository.load_transactions(db)[0]
    assert isinstance(restored.raw, dict)
    assert restored.raw["pending_transaction_id"] == "abc123"


def test_raw_is_read_by_field_not_compared_wholesale(db):
    """The decision made at 5.2, stated as a test. A Plaid row's dates went in
    as date objects and come back as strings, so dict equality FAILS by design
    -- what 5.7 depends on is one field, and that survives."""
    account = _account()
    original = {"pending_transaction_id": "abc123", "date": date(2026, 7, 27)}
    repository.save(db, [account], [_transaction(account=account, raw=original)])

    restored = repository.load_transactions(db)[0].raw
    assert restored != original
    assert restored["pending_transaction_id"] == original["pending_transaction_id"]
    assert restored["date"] == "2026-07-27"


def test_an_empty_raw_round_trips_exactly(db):
    account = _account()
    repository.save(db, [account], [_transaction(account=account)])
    assert repository.load_transactions(db)[0].raw == {}


def test_transactions_come_back_in_date_order(db):
    source = FakeSource()
    repository.save(db, source.fetch_accounts(), source.fetch_transactions().added)

    dates = [t.date for t in repository.load_transactions(db)]
    assert dates == sorted(dates)


def test_read_order_is_total_and_independent_of_write_order(db):
    """12 fixture dates carry ties, and main.py's recent list sorts with a
    stable sort -- so a tie order that depended on insertion order would make
    the render depend on the order rows happened to be written. (date, id) is
    a TOTAL order because id is unique, so it cannot."""
    source = FakeSource()
    accounts = source.fetch_accounts()
    transactions = source.fetch_transactions().added

    repository.save(db, accounts, transactions)
    forwards = [t.id for t in repository.load_transactions(db)]

    repository.save(db, accounts, list(reversed(transactions)))
    backwards = [t.id for t in repository.load_transactions(db)]

    assert forwards == backwards


def test_reading_an_empty_database_returns_empty_lists(db):
    assert repository.load_accounts(db) == []
    assert repository.load_transactions(db) == []


def test_the_read_path_returns_everything_unfiltered(db):
    """No filters by design: main.py needs all of it, and adding a date range
    or an account filter before something asks for one is guessing at a
    signature."""
    source = FakeSource()
    transactions = source.fetch_transactions().added
    repository.save(db, source.fetch_accounts(), transactions)

    assert len(repository.load_transactions(db)) == len(transactions)


def test_loaded_transactions_still_satisfy_model_invariants(db):
    """The read path builds real model objects, so __post_init__ runs on every
    row -- which means a corrupted amount or currency in the database fails on
    load rather than three layers up in a total."""
    source = FakeSource()
    repository.save(db, source.fetch_accounts(), source.fetch_transactions().added)

    for txn in repository.load_transactions(db):
        assert isinstance(txn.amount_cents, int)
        assert not isinstance(txn.amount_cents, bool)
        assert txn.merchant
        assert txn.currency == txn.currency.upper()


def test_insights_produce_the_same_answers_from_the_database(db):
    """The point of the whole step: analysis must not be able to tell where its
    input came from. Same functions, same numbers, adapter versus database."""
    from insights import find_recurring, spending_by_category

    source = FakeSource()
    accounts = source.fetch_accounts()
    transactions = source.fetch_transactions().added
    repository.save(db, accounts, transactions)
    loaded = repository.load_transactions(db)

    assert spending_by_category(loaded) == spending_by_category(transactions)
    assert find_recurring(loaded) == find_recurring(transactions)


def test_a_real_plaid_payload_round_trips(db):
    """The fake fixture carries an EMPTY raw on every row, so nothing above
    this exercises the archive on a real payload -- the floats, the nulls, the
    kilobytes, the 401k balance with four decimal places.

    Built from the recorded fixture through the translation functions directly
    rather than by importing tests/test_conformance.py, which pins DATA_SOURCE
    and CSV_PATH at import and would drag those side effects in here."""
    import json as _json

    from adapters.plaid_source import to_account, to_transaction

    payload = _json.loads(
        (store.PROJECT_ROOT / "fixtures" / "plaid_sandbox.json").read_text()
    )
    item_id = "item_test"
    accounts = [to_account(a, item_id) for a in payload["accounts"]]
    transactions = [to_transaction(t, item_id) for t in payload["transactions"]]

    repository.save(db, accounts, transactions)

    loaded = {t.id: t for t in repository.load_transactions(db)}
    assert len(loaded) == len(transactions)

    for original in transactions:
        restored = loaded[original.id]
        assert restored.amount_cents == original.amount_cents
        assert restored.merchant == original.merchant
        assert restored.kind == original.kind
        assert restored.date == original.date
        assert restored.source == original.source
        assert restored.raw["transaction_id"] == original.raw["transaction_id"]

    by_id = {a.id: a for a in repository.load_accounts(db)}
    for original in accounts:
        assert by_id[original.id] == original


def test_every_stored_transaction_joins_to_a_stored_account(db):
    """STRUCTURE's namespacing check, asked of the database. The FK makes this
    impossible to violate, which is the point of having it."""
    source = FakeSource()
    repository.save(db, source.fetch_accounts(), source.fetch_transactions().added)

    orphans = db.execute(
        "SELECT COUNT(*) c FROM transactions t "
        "LEFT JOIN accounts a ON a.id = t.account_id WHERE a.id IS NULL"
    ).fetchone()["c"]
    assert orphans == 0
