"""
tests/test_crypto.py
--------------------
Step 6.3, items 1-3: the schema column, the cipher, and the repository
functions around them.

The thing under test is one sentence: a Plaid access token grants live read
access to somebody's bank, does not expire, and must therefore never sit in the
database as plaintext or leave `core/repository.py` except to the caller that
asked for it.
"""

from __future__ import annotations

import sqlite3

import pytest

from core import crypto, repository, store, users
from core.models import ConnectionRef, Provider

TOKEN = "access-sandbox-11111111-2222-3333-4444-555555555555"
OTHER_TOKEN = "access-sandbox-99999999-8888-7777-6666-555555555555"

ITEM = ConnectionRef(provider=Provider.PLAID, connection_id="item_ada")
OTHER_ITEM = ConnectionRef(provider=Provider.PLAID, connection_id="item_bob")


@pytest.fixture
def db(tmp_path):
    conn = store.open_db(str(tmp_path / "crypto.db"))
    users.ensure_local_user(conn)
    yield conn
    conn.close()


@pytest.fixture
def ada(db):
    return users.create_user_with_hash(db, "ada@example.com", "hash")


@pytest.fixture
def bob(db):
    return users.create_user_with_hash(db, "bob@example.com", "hash")


# ---------------------------------------------------------------------------
# The cipher
# ---------------------------------------------------------------------------

def test_a_token_round_trips():
    assert crypto.decrypt(crypto.encrypt(TOKEN)) == TOKEN


def test_the_ciphertext_does_not_contain_the_plaintext():
    ciphertext = crypto.encrypt(TOKEN)

    assert TOKEN not in ciphertext
    assert "access-sandbox" not in ciphertext
    assert crypto.looks_encrypted(ciphertext)


def test_encrypting_twice_gives_different_ciphertext():
    """Fernet picks a fresh IV per call, so equal tokens do not produce equal
    rows -- otherwise the table would leak which two users linked the same
    Item, without either token being readable."""
    first = crypto.encrypt(TOKEN)
    second = crypto.encrypt(TOKEN)

    assert first != second
    assert crypto.decrypt(first) == crypto.decrypt(second) == TOKEN


def test_a_tampered_ciphertext_is_rejected_rather_than_returning_garbage():
    """Fernet is authenticated, which is the reason to use it over raw AES: a
    flipped byte fails loudly instead of decrypting to a corrupted token that
    would then be sent to Plaid."""
    ciphertext = crypto.encrypt(TOKEN)
    tampered = ciphertext[:-4] + ("AAAA" if ciphertext[-4:] != "AAAA" else "BBBB")

    with pytest.raises(crypto.EncryptionKeyInvalid):
        crypto.decrypt(tampered)


def test_a_token_encrypted_under_another_key_does_not_decrypt(monkeypatch):
    """What a key rotation looks like from the inside. It fails closed, and the
    message says which of the two causes it is."""
    ciphertext = crypto.encrypt(TOKEN)

    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    with pytest.raises(crypto.EncryptionKeyInvalid, match="rotated"):
        crypto.decrypt(ciphertext)


def test_a_missing_key_fails_loudly_and_says_how_to_make_one(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)

    assert crypto.is_configured() is False
    with pytest.raises(crypto.EncryptionKeyMissing) as caught:
        crypto.encrypt(TOKEN)

    assert "Fernet.generate_key" in str(caught.value)
    assert "ENCRYPTION_KEY" in str(caught.value)


def test_a_malformed_key_is_refused_with_its_own_message(monkeypatch):
    """Distinct from missing: a truncated paste is the likely mistake, and
    'not set' would be the wrong thing to go looking for."""
    monkeypatch.setenv("ENCRYPTION_KEY", "too-short")

    with pytest.raises(crypto.EncryptionKeyInvalid):
        crypto.encrypt(TOKEN)


def test_the_key_is_read_per_call_not_cached_at_import(monkeypatch):
    """Same rule as DB_PATH and DATA_SOURCE. A key frozen at import would make
    every test that changes it talk to nobody."""
    first = crypto.generate_key()
    monkeypatch.setenv("ENCRYPTION_KEY", first)
    ciphertext = crypto.encrypt(TOKEN)

    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    with pytest.raises(crypto.EncryptionKeyInvalid):
        crypto.decrypt(ciphertext)

    monkeypatch.setenv("ENCRYPTION_KEY", first)
    assert crypto.decrypt(ciphertext) == TOKEN


def test_an_empty_token_is_refused(monkeypatch):
    """A caller that lost the value would otherwise store a ciphertext that
    decrypts to '' -- unusable, and indistinguishable from a real one."""
    with pytest.raises(ValueError):
        crypto.encrypt("")


def test_a_generated_key_is_the_shape_the_docs_promise():
    key = crypto.generate_key()
    assert len(key) == crypto.FERNET_KEY_LENGTH
    assert key.isascii()


# ---------------------------------------------------------------------------
# The column
# ---------------------------------------------------------------------------

def test_the_access_token_column_lives_on_connections(db):
    columns = {r[1] for r in db.execute("PRAGMA table_info(connections)")}
    assert "access_token" in columns

    for table in ("accounts", "transactions", "users", "sessions"):
        other = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        assert "access_token" not in other, table


def test_a_source_without_a_token_stores_null(db):
    """Every source except Plaid. NULL rather than '' because 'this concept
    does not apply' and 'the token is the empty string' are different facts."""
    repository.save_connection(
        db, users.LOCAL_USER_ID,
        ConnectionRef(provider=Provider.FAKE, connection_id="fixture"),
    )
    db.commit()

    stored = db.execute("SELECT access_token FROM connections").fetchone()
    assert stored["access_token"] is None


def test_the_schema_refuses_a_plaintext_token(db, ada):
    """The last line of defence, and the one that does not depend on anybody
    remembering to call encrypt(). A CHECK on the Fernet prefix cannot verify
    the ciphertext, but it does catch the specific mistake worth catching: a
    raw access token written straight into the column."""
    repository.save_connection(db, ada, ITEM)
    db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE connections SET access_token = ? WHERE connection_id = ?",
            (TOKEN, ITEM.connection_id),
        )


def test_schema_version_is_eleven(db):
    assert store.SCHEMA_VERSION == 11
    row = db.execute("SELECT schema_version FROM meta WHERE id = 1").fetchone()
    assert row["schema_version"] == 11


# ---------------------------------------------------------------------------
# The repository functions
# ---------------------------------------------------------------------------

def test_a_token_is_encrypted_on_write_and_decrypted_on_read(db, ada):
    repository.save_access_token(db, ada, ITEM, TOKEN)
    db.commit()

    stored = db.execute("SELECT access_token FROM connections").fetchone()
    assert stored["access_token"] != TOKEN
    assert crypto.looks_encrypted(stored["access_token"])

    assert repository.access_token_for(db, ada, ITEM) == TOKEN


def test_the_plaintext_token_is_nowhere_in_the_database_file(db, ada, tmp_path):
    """Read as BYTES off disk, not through a SELECT. The point of encrypting
    is that somebody who copies the file gets nothing, and only the file
    itself can answer that."""
    path = tmp_path / "ondisk.db"
    conn = store.open_db(str(path))
    try:
        user_id = users.create_user_with_hash(conn, "ada@example.com", "hash")
        repository.save_access_token(conn, user_id, ITEM, TOKEN)
        conn.commit()
    finally:
        conn.close()

    raw = path.read_bytes()
    assert TOKEN.encode() not in raw
    assert b"access-sandbox" not in raw


def test_a_token_is_scoped_to_its_owner(db, ada, bob):
    """The same rule as every other repository function. Reading another
    user's token would be worse than reading their transactions -- it grants
    live access to the bank rather than a copy of what was synced."""
    repository.save_access_token(db, ada, ITEM, TOKEN)
    db.commit()

    assert repository.access_token_for(db, ada, ITEM) == TOKEN
    assert repository.access_token_for(db, bob, ITEM) is None


def test_two_users_tokens_are_independent(db, ada, bob):
    repository.save_access_token(db, ada, ITEM, TOKEN)
    repository.save_access_token(db, bob, OTHER_ITEM, OTHER_TOKEN)
    db.commit()

    assert repository.access_token_for(db, ada, ITEM) == TOKEN
    assert repository.access_token_for(db, bob, OTHER_ITEM) == OTHER_TOKEN
    assert repository.access_token_for(db, ada, OTHER_ITEM) is None
    assert repository.access_token_for(db, bob, ITEM) is None


def test_writing_a_token_onto_another_users_connection_is_refused(db, ada, bob):
    """`ensure_connections`' rule, reached through the token path. Without the
    scope on the UPDATE, bob could overwrite ada's token and point her sync at
    his Item."""
    repository.save_access_token(db, ada, ITEM, TOKEN)
    db.commit()

    with pytest.raises(ValueError, match="belongs to user"):
        repository.save_access_token(db, bob, ITEM, OTHER_TOKEN)

    assert repository.access_token_for(db, ada, ITEM) == TOKEN


def test_a_connection_with_no_matching_user_is_unreachable(db, ada):
    repository.save_access_token(db, ada, ITEM, TOKEN)
    db.commit()

    stranger = users.new_user_id()
    assert repository.access_token_for(db, stranger, ITEM) is None
    assert repository.connections_for(db, stranger) == []
    assert repository.linked_connections(db, stranger) == []


def test_an_absent_connection_reads_as_none_rather_than_raising(db, ada):
    assert repository.access_token_for(db, ada, ITEM) is None


def test_forgetting_the_user_id_is_a_type_error(db):
    for call in (
        lambda: repository.access_token_for(db, ITEM),
        lambda: repository.connections_for(db),
        lambda: repository.linked_connections(db),
        lambda: repository.save_access_token(db, ITEM, TOKEN),
    ):
        with pytest.raises(TypeError):
            call()


def test_a_token_can_be_replaced_by_relinking(db, ada):
    """Re-linking the same Item issues a new access token, and the old one
    stops working. The row has to move with it."""
    repository.save_access_token(db, ada, ITEM, TOKEN)
    repository.save_access_token(db, ada, ITEM, OTHER_TOKEN)
    db.commit()

    assert repository.access_token_for(db, ada, ITEM) == OTHER_TOKEN
    assert db.execute("SELECT COUNT(*) c FROM connections").fetchone()["c"] == 1


def test_saving_a_cursor_does_not_disturb_the_token(db, ada):
    """`save_connection` writes cursor and fetched_at, and runs on every sync.
    Clearing the token there would silently unlink the account, and the next
    sync would be the thing that noticed."""
    repository.save_access_token(db, ada, ITEM, TOKEN)
    repository.save_connection(db, ada, ITEM, cursor="cursor-1")
    db.commit()

    assert repository.access_token_for(db, ada, ITEM) == TOKEN
    assert repository.cursor_for(db, ada, ITEM) == "cursor-1"


def test_linked_connections_lists_only_the_ones_with_tokens(db, ada):
    """What the sync loop iterates. A fake or CSV connection has no token and
    must not appear -- there is nothing to build a PlaidSource from."""
    repository.save_connection(
        db, ada, ConnectionRef(provider=Provider.FAKE, connection_id="fixture")
    )
    repository.save_access_token(db, ada, ITEM, TOKEN)
    repository.save_access_token(
        db, ada, ConnectionRef(provider=Provider.PLAID, connection_id="item_2"),
        OTHER_TOKEN,
    )
    db.commit()

    assert len(repository.connections_for(db, ada)) == 3

    linked = repository.linked_connections(db, ada)
    assert [ref.connection_id for ref, _ in linked] == ["item_2", "item_ada"]
    assert sorted(token for _, token in linked) == sorted([TOKEN, OTHER_TOKEN])


def test_owner_of_reports_the_user_a_connection_already_belongs_to(db, ada):
    """What the exchange endpoint needs to tell 'you already linked this' from
    'somebody else did' -- two answers that must not be the same 409."""
    repository.save_access_token(db, ada, ITEM, TOKEN)
    db.commit()

    assert repository.owner_of(db, ITEM) == ada
    assert repository.owner_of(db, OTHER_ITEM) is None
