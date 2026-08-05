"""
tests/test_users.py
-------------------
Step 6.1: users, and the scoping that is the whole point of it.

The suite splits in two. The first half is about the user record itself -- what
a user id is, what makes an email unique, what the local user's password hash
means. The second half is the half that matters: every repository query filters
by user, and a query that forgot to would still pass every test written before
this file existed, because there has only ever been one user in the database.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime

import pytest

from core import repository, store, users
from core.models import (
    Account,
    AccountType,
    ConnectionRef,
    Provider,
    SourceRef,
    Transaction,
    make_id,
)


@pytest.fixture
def db(tmp_path):
    conn = store.open_db(str(tmp_path / "test.db"))
    users.ensure_local_user(conn)
    yield conn
    conn.close()


@pytest.fixture
def other(db):
    """A second user, owning a DIFFERENT connection.

    Different by necessity, not by taste: ids carry no user segment, so two
    users sharing one connection_id would mint colliding account ids. See
    test_a_connection_belongs_to_exactly_one_user.
    """
    user_id = users.create_user_with_hash(db, "other@example.com", "argon2:whatever")
    db.commit()
    return user_id


def _account(connection_id="fixture", provider_id="acc_1", **kwargs) -> Account:
    ref = SourceRef(
        provider=Provider.FAKE,
        provider_id=provider_id,
        connection_id=connection_id,
    )
    fields = dict(
        id=make_id(ref),
        source=ref,
        name="Chase Checking",
        institution="Chase",
        type=AccountType.CHECKING,
        balance_cents=100_000,
    )
    fields.update(kwargs)
    return Account(**fields)


def _transaction(account, provider_id="txn_1", **kwargs) -> Transaction:
    ref = SourceRef(
        provider=Provider.FAKE,
        provider_id=provider_id,
        connection_id=account.source.connection_id,
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


def _seed(db, user_id, connection_id, provider_id="acc_1"):
    account = _account(connection_id=connection_id, provider_id=provider_id)
    txn = _transaction(account, provider_id=f"txn_{provider_id}")
    repository.save(db, user_id, [account], [txn])
    return account, txn


# ---------------------------------------------------------------------------
# What a user id is
# ---------------------------------------------------------------------------

def test_a_user_id_is_a_uuid4_in_canonical_form(db):
    user_id = users.create_user_with_hash(db, "a@example.com", "hash")

    parsed = uuid.UUID(user_id)
    assert parsed.version == 4
    assert str(parsed) == user_id
    assert user_id == user_id.lower()
    assert len(user_id) == users.USER_ID_LENGTH


def test_user_ids_do_not_repeat():
    minted = {users.new_user_id() for _ in range(1000)}
    assert len(minted) == 1000


def test_a_user_id_is_not_an_enumerable_counter(db):
    """The reason it is a UUID rather than an INTEGER PRIMARY KEY.

    A sequential id in a URL or a log line tells anyone holding it how many
    users exist and what the neighbouring ids are, and it makes two databases
    impossible to merge -- user 1 in one file is a different person from user 1
    in the other. Neither is a problem you can retrofit once ids are in
    circulation.
    """
    first = users.create_user_with_hash(db, "a@example.com", "hash")
    second = users.create_user_with_hash(db, "b@example.com", "hash")

    assert not first.isdigit()
    assert abs(int(uuid.UUID(first)) - int(uuid.UUID(second))) > 2 ** 64


def test_the_id_is_minted_by_the_app_not_the_database(db):
    """SQLite has no uuid() function, so an id generated 'by the schema' would
    have to be a rowid -- and the caller could not know it before the INSERT."""
    chosen = users.new_user_id()
    returned = users.create_user_with_hash(db, "a@example.com", "hash", user_id=chosen)

    assert returned == chosen
    assert users.user_exists(db, chosen)


def test_a_malformed_user_id_is_refused_by_the_schema(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO users (id, email, password_hash, created_at) "
            "VALUES ('7', 'x@example.com', 'h', '2026-08-05T09:00:00')"
        )


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def test_email_is_stored_normalized(db):
    users.create_user_with_hash(db, "  Ada@Example.COM ", "hash")

    stored = db.execute("SELECT email FROM users WHERE email LIKE 'ada%'").fetchone()
    assert stored["email"] == "ada@example.com"


def test_two_users_cannot_share_an_email(db):
    users.create_user_with_hash(db, "ada@example.com", "hash")
    with pytest.raises(users.EmailAlreadyRegistered):
        users.create_user_with_hash(db, "ada@example.com", "hash")


def test_case_alone_does_not_make_a_second_account(db):
    """Normalizing on the way in rather than declaring the column NOCASE.

    NOCASE would work here and is banned everywhere else in this schema (it
    merges distinct ids), so the same file would be telling two stories about
    what a case-insensitive column means. Normalizing keeps every comparison
    exact and puts the one place casing is decided in Python.
    """
    users.create_user_with_hash(db, "ada@example.com", "hash")
    with pytest.raises(users.EmailAlreadyRegistered):
        users.create_user_with_hash(db, "ADA@EXAMPLE.COM", "hash")


def test_an_empty_email_is_refused(db):
    with pytest.raises(ValueError):
        users.create_user_with_hash(db, "   ", "hash")


def test_lookup_by_email_normalizes_too(db):
    user_id = users.create_user_with_hash(db, "ada@example.com", "hash")
    assert users.user_id_for_email(db, "  ADA@Example.com ") == user_id
    assert users.user_id_for_email(db, "nobody@example.com") is None


# ---------------------------------------------------------------------------
# The local user: scaffolding, and honest about it
# ---------------------------------------------------------------------------

def test_the_local_user_id_is_a_fixed_constant(db):
    """It cannot be minted per process. A fresh uuid4 on every run would file
    each sync under a new owner, and the render -- which looks the rows up by
    this constant -- would report an empty database the moment it changed."""
    assert users.ensure_local_user(db) == users.LOCAL_USER_ID
    assert uuid.UUID(users.LOCAL_USER_ID).version == 4


def test_ensure_local_user_is_idempotent(db):
    users.ensure_local_user(db)
    users.ensure_local_user(db)

    count = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    assert count == 1


def test_the_local_user_cannot_be_logged_into(db):
    """`!` is the Unix convention for a disabled password, and no hash function
    produces it -- so the scaffolding row cannot be authenticated against by
    accident once a verifier exists. The verifier's job is to check this."""
    stored = db.execute(
        "SELECT password_hash FROM users WHERE id = ?", (users.LOCAL_USER_ID,)
    ).fetchone()["password_hash"]

    assert stored == users.UNUSABLE_PASSWORD_HASH
    assert not users.is_usable_password_hash(stored)
    assert users.is_usable_password_hash("argon2id$v=19$...")


def test_an_empty_password_hash_is_refused(db):
    """Distinct from the unusable one. An empty hash is a caller that forgot to
    hash; `!` is a caller that meant it."""
    with pytest.raises(ValueError):
        users.create_user_with_hash(db, "a@example.com", "")


def test_rendering_reads_the_same_user_sync_writes(db):
    """The two commands share exactly one constant, and this is it. If they
    drifted, sync would write rows the dashboard could never find -- and the
    screen would say 'No data yet' with a full database on disk."""
    import main as app
    import sync as sync_module

    assert app.users.LOCAL_USER_ID == sync_module.users.LOCAL_USER_ID


# ---------------------------------------------------------------------------
# Scoping: the point of the step
# ---------------------------------------------------------------------------

def _spend(db, user_id, connection_id, charges, tag):
    """One account per user, and charges at merchants BOTH users shop at."""
    account = _account(connection_id=connection_id, provider_id=f"acc_{tag}")
    txns = [
        _transaction(
            account,
            provider_id=f"txn_{tag}_{i}",
            merchant=merchant,
            name=merchant.upper(),
            category=category,
            amount_cents=cents,
            date=date(2026, 7, 1 + i),
        )
        for i, (merchant, category, cents) in enumerate(charges)
    ]
    repository.save(db, user_id, [account], txns)
    return account, txns


#: The same three merchants for both users, at different amounts. Overlapping
#: names are what make a leak hard to see: distinct merchants would show up as
#: an obviously foreign row, while these blend into a total that still looks
#: like a plausible month.
SHARED_MERCHANTS = ("Netflix", "Chipotle", "Amazon")


def test_a_query_scoped_to_one_user_cannot_return_the_others_rows(db, other):
    """THE test for 6.1, on the case that hides rather than the case that
    announces itself.

    Both users shop at Netflix, Chipotle and Amazon, so a missing WHERE clause
    does not produce a strange unexplained merchant -- it produces the RIGHT
    merchants at doubled amounts, which reads as a busy month. Every assertion
    below is on amounts for that reason.
    """
    mine = [
        ("Netflix", "Entertainment", 1599),
        ("Chipotle", "Food & Drink", 1442),
        ("Amazon", "Shopping", 3210),
    ]
    theirs = [
        ("Netflix", "Entertainment", 2499),
        ("Chipotle", "Food & Drink", 987),
        ("Amazon", "Shopping", 45_600),
    ]
    _spend(db, users.LOCAL_USER_ID, "fixture", mine, "mine")
    _spend(db, other, "other_conn", theirs, "theirs")

    my_rows = repository.load_transactions(db, users.LOCAL_USER_ID)
    their_rows = repository.load_transactions(db, other)

    assert sorted(t.merchant for t in my_rows) == sorted(SHARED_MERCHANTS)
    assert sorted(t.merchant for t in their_rows) == sorted(SHARED_MERCHANTS)

    assert sorted(t.amount_cents for t in my_rows) == sorted(c for _, _, c in mine)
    assert sorted(t.amount_cents for t in their_rows) == sorted(c for _, _, c in theirs)

    assert sum(t.amount_cents for t in my_rows) == 6251
    assert sum(t.amount_cents for t in their_rows) == 49_086


def test_an_unscoped_read_would_be_visible_in_the_spending_table(db, other):
    """The same leak, asked of the thing a person actually looks at.

    `spending_by_category` is a sum over whatever it is handed, so it cannot
    defend itself -- the scoping has to have happened before insights sees the
    rows. Without the WHERE clause every category here doubles while every
    category NAME stays correct.
    """
    from insights import spending_by_category

    _spend(db, users.LOCAL_USER_ID, "fixture", [
        ("Netflix", "Entertainment", 1599),
        ("Chipotle", "Food & Drink", 1442),
    ], "mine")
    _spend(db, other, "other_conn", [
        ("Netflix", "Entertainment", 2499),
        ("Chipotle", "Food & Drink", 987),
    ], "theirs")

    assert spending_by_category(
        repository.load_transactions(db, users.LOCAL_USER_ID)
    ) == [("Entertainment", 1599), ("Food & Drink", 1442)]

    assert spending_by_category(
        repository.load_transactions(db, other)
    ) == [("Entertainment", 2499), ("Food & Drink", 987)]


def test_the_net_worth_of_one_user_excludes_the_others_accounts(db, other):
    """Balances are the other half. Two Chase Checking accounts, same
    institution and same name, distinguishable only by owner -- so an unscoped
    accounts read adds a stranger's money to the panel that claims to total
    'across all banks'."""
    repository.save(db, users.LOCAL_USER_ID, [
        _account(connection_id="fixture", provider_id="acc_mine",
                 balance_cents=100_000)
    ], [])
    repository.save(db, other, [
        _account(connection_id="other_conn", provider_id="acc_theirs",
                 balance_cents=7_500_000)
    ], [])

    mine = repository.load_accounts(db, users.LOCAL_USER_ID)
    theirs = repository.load_accounts(db, other)

    assert [a.name for a in mine] == [a.name for a in theirs] == ["Chase Checking"]
    assert sum(a.balance_cents for a in mine) == 100_000
    assert sum(a.balance_cents for a in theirs) == 7_500_000


def test_a_user_reads_only_their_own_rows(db, other):
    mine, my_txn = _seed(db, users.LOCAL_USER_ID, "fixture", "acc_mine")
    theirs, their_txn = _seed(db, other, "other_conn", "acc_theirs")

    assert [a.id for a in repository.load_accounts(db, users.LOCAL_USER_ID)] == [mine.id]
    assert [a.id for a in repository.load_accounts(db, other)] == [theirs.id]
    assert [t.id for t in repository.load_transactions(db, users.LOCAL_USER_ID)] == [my_txn.id]
    assert [t.id for t in repository.load_transactions(db, other)] == [their_txn.id]


def test_the_database_holds_both_users_rows(db, other):
    """The companion to the test above, and the reason it means anything: the
    rows ARE there, and the filter is what hides them. Without this, a query
    that returned nothing at all would look like correct isolation."""
    _seed(db, users.LOCAL_USER_ID, "fixture", "acc_mine")
    _seed(db, other, "other_conn", "acc_theirs")

    assert db.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 2
    assert db.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"] == 2


def test_an_unknown_user_reads_nothing(db):
    _seed(db, users.LOCAL_USER_ID, "fixture")

    stranger = users.new_user_id()
    assert repository.load_accounts(db, stranger) == []
    assert repository.load_transactions(db, stranger) == []
    assert repository.stored_providers(db, stranger) == []


def test_the_header_provenance_is_scoped_too(db, other):
    """`stored_providers` feeds the header. Unscoped, one user's Plaid link
    would relabel the other user's screen."""
    _seed(db, users.LOCAL_USER_ID, "fixture")

    csv_ref = SourceRef(
        provider=Provider.CSV, provider_id="acc_csv", connection_id="import_1",
    )
    repository.save(db, other, [_account(
        connection_id="import_1", provider_id="acc_csv",
        id=make_id(csv_ref), source=csv_ref,
    )], [])

    assert repository.stored_providers(db, users.LOCAL_USER_ID) == ["fake"]
    assert repository.stored_providers(db, other) == ["csv"]


def test_forgetting_the_user_id_is_a_type_error(db):
    """THE design decision of the step, stated as a test.

    user_id is a required positional argument rather than a keyword with a
    default, so a call site that forgets it fails immediately and loudly. The
    alternative -- defaulting to the local user, or to None meaning 'all' --
    turns the same mistake into a silent full-table read that returns plausible
    data belonging to somebody else.
    """
    for call in (
        lambda: repository.load_accounts(db),
        lambda: repository.load_transactions(db),
        lambda: repository.stored_providers(db),
        lambda: repository.save(db, [], []),
    ):
        with pytest.raises(TypeError):
            call()


# ---------------------------------------------------------------------------
# Scoping: writes and deletes
# ---------------------------------------------------------------------------

def test_one_user_cannot_delete_another_users_transaction(db, other):
    """`DELETE ... WHERE id = ?` is scoped by the same join the reads use.
    Unscoped it would succeed, report success, and remove somebody else's row
    -- an id is enough to name it, because ids carry no user segment."""
    _, their_txn = _seed(db, other, "other_conn", "acc_theirs")
    _seed(db, users.LOCAL_USER_ID, "fixture", "acc_mine")

    result = repository.save(
        db, users.LOCAL_USER_ID, [], [], removed=[their_txn.id]
    )

    assert result.deleted == 0
    assert result.unmatched == (their_txn.id,)
    assert len(repository.load_transactions(db, other)) == 1


def test_a_cursor_is_scoped_to_its_owner(db, other):
    """Two users, two Items, one resume token each. A cross-user read here
    would resume one user's sync from the other's position and silently skip
    every transaction in between."""
    mine = ConnectionRef(provider=Provider.FAKE, connection_id="fixture")
    theirs = ConnectionRef(provider=Provider.PLAID, connection_id="item_other")

    repository.save_connection(db, users.LOCAL_USER_ID, mine, cursor="mine-1")
    repository.save_connection(db, other, theirs, cursor="theirs-1")
    db.commit()

    assert repository.cursor_for(db, users.LOCAL_USER_ID, mine) == "mine-1"
    assert repository.cursor_for(db, users.LOCAL_USER_ID, theirs) is None
    assert repository.cursor_for(db, other, theirs) == "theirs-1"


def test_fetched_at_is_scoped_to_its_owner(db, other):
    mine = ConnectionRef(provider=Provider.FAKE, connection_id="fixture")
    repository.save(db, users.LOCAL_USER_ID, [_account()], [], connection=mine,
                    synced_at=datetime(2026, 7, 27, 14, 3, 11))

    assert repository.fetched_at_for(db, other, mine) is None
    assert repository.fetched_at_for(db, users.LOCAL_USER_ID, mine) == datetime(
        2026, 7, 27, 14, 3, 11
    )


# ---------------------------------------------------------------------------
# The chain: accounts -> connections -> users
# ---------------------------------------------------------------------------

def test_user_id_is_stored_once_per_connection_not_once_per_row(db):
    """The denormalization this schema deliberately did not make.

    Copying user_id onto every account and transaction would write one value 76
    times per sync, and would then need keeping consistent with the connection
    it was copied from -- the same mistake `fetched_at` was moved out of these
    tables to stop at 5.8.
    """
    account_columns = {r[1] for r in db.execute("PRAGMA table_info(accounts)")}
    txn_columns = {r[1] for r in db.execute("PRAGMA table_info(transactions)")}
    connection_columns = {
        r[1] for r in db.execute("PRAGMA table_info(connections)")
    }

    assert "user_id" not in account_columns
    assert "user_id" not in txn_columns
    assert "user_id" in connection_columns


def test_an_account_cannot_exist_without_its_connection(db):
    """What makes the chain a chain rather than a convention.

    Without this FK an account whose connections row is missing joins to
    nothing and vanishes from every query -- present in the file, absent from
    the dashboard, and reported by no error at all.
    """
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO accounts "
            "(id, provider, provider_id, connection_id, name, institution, "
            " type, balance_cents) "
            "VALUES ('x', 'fake', 'a', 'no_such_connection', 'n', 'i', "
            "        'checking', 100)"
        )


def test_a_connection_cannot_belong_to_a_user_that_does_not_exist(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO connections (provider, connection_id, user_id) "
            "VALUES ('fake', 'orphan', ?)",
            (users.new_user_id(),),
        )


def test_a_connection_belongs_to_exactly_one_user(db, other):
    """The cross-user write, refused loudly instead of silently.

    An id is `provider:connection_id:provider_id` and carries no user segment,
    so two users syncing one connection_id would mint IDENTICAL account ids --
    and the second sync's upsert would overwrite the first user's rows rather
    than sit beside them. Refusing at the connection is what makes that
    impossible; the alternative is data loss with no error.
    """
    _seed(db, users.LOCAL_USER_ID, "fixture")

    with pytest.raises(ValueError, match="belongs to user"):
        repository.save(db, other, [_account(connection_id="fixture")], [])

    assert repository.load_accounts(db, other) == []
    assert len(repository.load_accounts(db, users.LOCAL_USER_ID)) == 1


def test_a_sync_creates_the_connection_it_needs(db):
    """`save` is called without a ConnectionRef all over this suite and by the
    baseline gate. The connections row is derived from the records' own
    provenance so that path cannot leave accounts orphaned."""
    repository.save(db, users.LOCAL_USER_ID, [_account()], [])

    row = db.execute(
        "SELECT user_id, cursor, fetched_at FROM connections"
    ).fetchone()
    assert row["user_id"] == users.LOCAL_USER_ID
    assert row["cursor"] is None
    assert row["fetched_at"] is None


def test_a_rejected_cross_user_write_leaves_nothing_behind(db, other):
    _seed(db, users.LOCAL_USER_ID, "fixture")

    with pytest.raises(ValueError):
        repository.save(
            db, other,
            [_account(connection_id="fixture", provider_id="acc_2")],
            [],
        )

    assert db.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 1
