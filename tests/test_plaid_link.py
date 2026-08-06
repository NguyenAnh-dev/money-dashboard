"""
tests/test_plaid_link.py
------------------------
Step 6.3, items 4-8: the Link handshake over HTTP.

No network. The two Plaid calls are stubbed, but at two different depths on
purpose:

  * The SDK-shape tests pass a STUB CLIENT into the real
    `create_link_token` / `exchange_public_token`, so the plaid-python request
    models are genuinely constructed. A wrong product string or a renamed
    field fails there rather than the first time somebody runs it for real.
  * The endpoint tests replace those two functions outright, because what they
    are about is the session, the scoping and the response body -- not Plaid.

The browser half (Link's JS SDK) is the frontend phase and is not here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from adapters import plaid_source
from api.app import app
from api.deps import get_db
from core import crypto, repository, sessions, store, users
from core.models import ConnectionRef, Provider

PUBLIC_TOKEN = "public-sandbox-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ACCESS_TOKEN = "access-sandbox-11111111-2222-3333-4444-555555555555"
OTHER_ACCESS_TOKEN = "access-sandbox-99999999-8888-7777-6666-555555555555"
ITEM_ID = "item_ada_11111"
OTHER_ITEM_ID = "item_bob_99999"
LINK_TOKEN = "link-sandbox-abcdefab-1234-5678-9abc-def012345678"


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "plaid.db")


@pytest.fixture
def db(db_path):
    conn = store.open_db(db_path)
    yield conn
    conn.close()


@pytest.fixture
def client(db, db_path):
    def override():
        conn = store.open_db(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def ada(db):
    return users.create_user_with_hash(db, "ada@example.com", "hash")


@pytest.fixture
def bob(db):
    return users.create_user_with_hash(db, "bob@example.com", "hash")


def _as(client, token=None):
    client.cookies.clear()
    if token is not None:
        client.cookies.set("session", token)
    return client


def _session(db, user_id):
    return sessions.create_session(db, user_id)


@pytest.fixture
def plaid_calls(monkeypatch):
    """Stand in for the two network calls, and record what they were asked."""
    calls = {"link": [], "exchange": [], "access_token": ACCESS_TOKEN,
             "item_id": ITEM_ID}

    def fake_link_token(client_user_id, client=None):
        calls["link"].append(client_user_id)
        return LINK_TOKEN

    def fake_exchange(public_token, client=None):
        calls["exchange"].append(public_token)
        return calls["access_token"], calls["item_id"]

    monkeypatch.setattr(plaid_source, "create_link_token", fake_link_token)
    monkeypatch.setattr(plaid_source, "exchange_public_token", fake_exchange)
    return calls


# ---------------------------------------------------------------------------
# The SDK request shapes, against a stub client
# ---------------------------------------------------------------------------

class _StubClient:
    def __init__(self):
        self.link_request = None
        self.exchange_request = None

    def link_token_create(self, request):
        self.link_request = request
        return {"link_token": LINK_TOKEN, "expiration": "2026-08-06T12:00:00Z"}

    def item_public_token_exchange(self, request):
        self.exchange_request = request
        return {"access_token": ACCESS_TOKEN, "item_id": ITEM_ID}


def test_the_link_request_carries_our_user_id_and_nothing_else_identifying():
    """`client_user_id` is what makes a Link session belong to somebody, and it
    goes to a third party -- which is one of the reasons a user id is a UUID
    rather than an email."""
    stub = _StubClient()
    user_id = users.new_user_id()

    assert plaid_source.create_link_token(user_id, client=stub) == LINK_TOKEN

    request = stub.link_request
    assert request["user"]["client_user_id"] == user_id
    assert "@" not in str(request["user"])


def test_link_asks_only_for_the_product_this_app_reads():
    """Asking for more widens the bank's consent screen for data nothing uses,
    and Plaid bills per product."""
    stub = _StubClient()
    plaid_source.create_link_token(users.new_user_id(), client=stub)

    assert [str(p) for p in stub.link_request["products"]] == ["transactions"]
    assert [str(c) for c in stub.link_request["country_codes"]] == ["US"]


def test_the_exchange_request_sends_the_public_token():
    stub = _StubClient()

    access_token, item_id = plaid_source.exchange_public_token(
        PUBLIC_TOKEN, client=stub
    )

    assert stub.exchange_request["public_token"] == PUBLIC_TOKEN
    assert (access_token, item_id) == (ACCESS_TOKEN, ITEM_ID)


def test_building_a_client_refuses_anything_but_sandbox(monkeypatch):
    monkeypatch.setenv("PLAID_ENV", "production")
    with pytest.raises(ValueError, match="sandbox"):
        plaid_source.build_client()


def test_missing_credentials_name_themselves(monkeypatch):
    """Found by running the server for real. Unset, these reach urllib3 as
    None and die in http.client with "expected string or bytes-like object,
    got 'NoneType'" -- a traceback naming neither Plaid nor the variable, three
    libraries below the code that asked."""
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    monkeypatch.delenv("PLAID_CLIENT_ID", raising=False)
    monkeypatch.delenv("PLAID_SECRET", raising=False)

    with pytest.raises(ValueError) as caught:
        plaid_source.build_client()

    assert "PLAID_CLIENT_ID" in str(caught.value)
    assert "PLAID_SECRET" in str(caught.value)
    assert "--env-file" in str(caught.value)


# ---------------------------------------------------------------------------
# POST /plaid/link-token
# ---------------------------------------------------------------------------

def test_link_token_requires_a_session(client, plaid_calls):
    assert _as(client).post("/plaid/link-token").status_code == 401
    assert plaid_calls["link"] == [], "no Plaid call for an anonymous request"


def test_link_token_is_minted_for_the_session_holder(client, db, ada, bob,
                                                     plaid_calls):
    """The user id Plaid is told about comes from the cookie, never from the
    request -- otherwise a Link session could be opened on someone else's
    behalf and the resulting Item would attach to them."""
    response = _as(client, _session(db, ada)).post("/plaid/link-token")

    assert response.status_code == 200
    assert response.json() == {"link_token": LINK_TOKEN}
    assert plaid_calls["link"] == [ada]

    _as(client, _session(db, bob)).post("/plaid/link-token")
    assert plaid_calls["link"] == [ada, bob]


# ---------------------------------------------------------------------------
# POST /plaid/exchange
# ---------------------------------------------------------------------------

def test_exchange_requires_a_session(client, db, plaid_calls):
    response = _as(client).post(
        "/plaid/exchange", json={"public_token": PUBLIC_TOKEN}
    )

    assert response.status_code == 401
    assert plaid_calls["exchange"] == []
    assert db.execute("SELECT COUNT(*) c FROM connections").fetchone()["c"] == 0


def test_exchange_stores_the_connection_against_the_session_holder(
    client, db, ada, plaid_calls
):
    response = _as(client, _session(db, ada)).post(
        "/plaid/exchange", json={"public_token": PUBLIC_TOKEN}
    )

    assert response.status_code == 201
    assert response.json() == {
        "provider": "plaid", "connection_id": ITEM_ID, "linked": True,
    }
    assert plaid_calls["exchange"] == [PUBLIC_TOKEN]

    connection = ConnectionRef(provider=Provider.PLAID, connection_id=ITEM_ID)
    assert repository.owner_of(db, connection) == ada
    assert repository.access_token_for(db, ada, connection) == ACCESS_TOKEN


def test_the_stored_token_is_encrypted(client, db, ada, plaid_calls):
    _as(client, _session(db, ada)).post(
        "/plaid/exchange", json={"public_token": PUBLIC_TOKEN}
    )

    stored = db.execute("SELECT access_token FROM connections").fetchone()
    assert stored["access_token"] != ACCESS_TOKEN
    assert crypto.looks_encrypted(stored["access_token"])


def test_an_empty_public_token_is_rejected_before_plaid_is_called(
    client, db, ada, plaid_calls
):
    response = _as(client, _session(db, ada)).post(
        "/plaid/exchange", json={"public_token": ""}
    )

    assert response.status_code == 422
    assert plaid_calls["exchange"] == []


# ---------------------------------------------------------------------------
# THE tests item 8 names
# ---------------------------------------------------------------------------

def test_the_access_token_never_appears_in_any_response_body(
    client, db, ada, plaid_calls
):
    """The one that matters most. A token in a response body is a token in
    somebody's browser history, in a proxy log, and in every error reporter
    the frontend uses -- and unlike a session it cannot be revoked by logging
    out, because it does not expire at all.
    """
    token = _session(db, ada)
    bodies = [
        _as(client, token).post("/plaid/link-token").text,
        _as(client, token).post(
            "/plaid/exchange", json={"public_token": PUBLIC_TOKEN}
        ).text,
        _as(client, token).get("/plaid/connections").text,
        _as(client, token).get("/accounts").text,
        _as(client, token).get("/transactions").text,
        _as(client, token).get("/spending").text,
        _as(client, token).get("/recurring").text,
        _as(client, token).get("/auth/me").text,
    ]

    for body in bodies:
        assert ACCESS_TOKEN not in body
        assert "access-sandbox" not in body
        assert "access_token" not in body


def test_the_ciphertext_does_not_leak_either(client, db, ada, plaid_calls):
    """Weaker than the plaintext but still worth nothing to a client, and
    handing it out would make the encryption a formality the moment the key
    leaked by any other route."""
    token = _session(db, ada)
    _as(client, token).post(
        "/plaid/exchange", json={"public_token": PUBLIC_TOKEN}
    )
    stored = db.execute("SELECT access_token FROM connections").fetchone()

    body = _as(client, token).get("/plaid/connections").text
    assert stored["access_token"] not in body
    assert "gAAAAA" not in body


def test_a_user_can_only_exchange_into_their_own_connections(
    client, db, ada, bob, plaid_calls
):
    """Two people linking the SAME Item. The second is refused rather than
    silently taking it over -- an Item id is the connection id, so allowing it
    would let bob overwrite ada's token and point her sync at his credentials
    (or, read the other way, quietly inherit hers)."""
    _as(client, _session(db, ada)).post(
        "/plaid/exchange", json={"public_token": PUBLIC_TOKEN}
    )

    response = _as(client, _session(db, bob)).post(
        "/plaid/exchange", json={"public_token": PUBLIC_TOKEN}
    )

    assert response.status_code == 409
    assert "another account" in response.json()["detail"]

    connection = ConnectionRef(provider=Provider.PLAID, connection_id=ITEM_ID)
    assert repository.owner_of(db, connection) == ada
    assert repository.access_token_for(db, ada, connection) == ACCESS_TOKEN
    assert repository.access_token_for(db, bob, connection) is None


def test_relinking_your_own_item_replaces_the_token(client, db, ada,
                                                    plaid_calls):
    """Not the same case as somebody else's Item, and it must not be a 409:
    re-running Link on an account you already hold issues a NEW access token,
    and the old one stops working."""
    _as(client, _session(db, ada)).post(
        "/plaid/exchange", json={"public_token": PUBLIC_TOKEN}
    )

    plaid_calls["access_token"] = OTHER_ACCESS_TOKEN
    response = _as(client, _session(db, ada)).post(
        "/plaid/exchange", json={"public_token": PUBLIC_TOKEN}
    )

    assert response.status_code == 201
    connection = ConnectionRef(provider=Provider.PLAID, connection_id=ITEM_ID)
    assert repository.access_token_for(db, ada, connection) == OTHER_ACCESS_TOKEN
    assert db.execute("SELECT COUNT(*) c FROM connections").fetchone()["c"] == 1


def test_two_users_tokens_are_independent(client, db, ada, bob, plaid_calls):
    _as(client, _session(db, ada)).post(
        "/plaid/exchange", json={"public_token": PUBLIC_TOKEN}
    )

    plaid_calls["access_token"] = OTHER_ACCESS_TOKEN
    plaid_calls["item_id"] = OTHER_ITEM_ID
    _as(client, _session(db, bob)).post(
        "/plaid/exchange", json={"public_token": PUBLIC_TOKEN}
    )

    ada_item = ConnectionRef(provider=Provider.PLAID, connection_id=ITEM_ID)
    bob_item = ConnectionRef(provider=Provider.PLAID,
                             connection_id=OTHER_ITEM_ID)

    assert repository.access_token_for(db, ada, ada_item) == ACCESS_TOKEN
    assert repository.access_token_for(db, bob, bob_item) == OTHER_ACCESS_TOKEN
    assert repository.access_token_for(db, ada, bob_item) is None
    assert repository.access_token_for(db, bob, ada_item) is None


def test_a_connection_with_no_matching_user_is_unreachable(
    client, db, ada, bob, plaid_calls
):
    """From every direction: the listing, the repository, and the sync loop."""
    _as(client, _session(db, ada)).post(
        "/plaid/exchange", json={"public_token": PUBLIC_TOKEN}
    )

    hers = _as(client, _session(db, ada)).get("/plaid/connections").json()
    his = _as(client, _session(db, bob)).get("/plaid/connections").json()

    assert [c["connection_id"] for c in hers] == [ITEM_ID]
    assert his == []

    stranger = users.new_user_id()
    assert repository.connections_for(db, stranger) == []
    assert repository.linked_connections(db, stranger) == []

    import sync as sync_module
    assert sync_module.linked_sources(db, bob) == []
    assert len(sync_module.linked_sources(db, ada)) == 1


# ---------------------------------------------------------------------------
# The sync loop: one user, several Items
# ---------------------------------------------------------------------------

class _StubSource:
    """A PlaidSource with the network taken out. Records the cursor it was
    handed, which is the thing the loop has to get right per connection."""

    built = []

    def __init__(self, access_token, item_id=None, client=None):
        self.access_token = access_token
        self.item_id = item_id
        self.cursors = []
        _StubSource.built.append(self)

    def connection(self):
        return ConnectionRef(provider=Provider.PLAID,
                             connection_id=self.item_id)

    def fetch_accounts(self):
        return []

    def fetch_transactions(self, cursor=None):
        from core.models import SyncResult

        self.cursors.append(cursor)
        return SyncResult(cursor=f"cursor-after-{self.item_id}")


@pytest.fixture
def stub_plaid_source(monkeypatch):
    _StubSource.built = []
    monkeypatch.setattr(plaid_source, "PlaidSource", _StubSource)
    return _StubSource


def _link(db, user_id, item_id, token):
    repository.save_access_token(
        db, user_id,
        ConnectionRef(provider=Provider.PLAID, connection_id=item_id),
        token,
    )
    db.commit()


def test_sync_loops_over_every_connection_a_user_has(db, ada,
                                                     stub_plaid_source):
    """Item 6. Before this the sync path assumed exactly one source, so a
    second linked bank was simply never fetched -- silently, since the first
    one still synced and the dashboard still rendered."""
    import sync as sync_module

    _link(db, ada, "item_1", ACCESS_TOKEN)
    _link(db, ada, "item_2", OTHER_ACCESS_TOKEN)

    sources = sync_module.linked_sources(db, ada)

    assert len(sources) == 2
    assert sorted(s.item_id for s in sources) == ["item_1", "item_2"]
    assert sorted(s.access_token for s in sources) == sorted(
        [ACCESS_TOKEN, OTHER_ACCESS_TOKEN]
    )


def test_each_connection_is_handed_its_own_cursor(db, ada, stub_plaid_source):
    """The cursors were already per connection in the schema; this is what
    makes the loop honour that. Handing every Item the same token would
    resume one from another's position and silently skip transactions."""
    import sync as sync_module

    _link(db, ada, "item_1", ACCESS_TOKEN)
    _link(db, ada, "item_2", OTHER_ACCESS_TOKEN)
    repository.save_connection(
        db, ada, ConnectionRef(provider=Provider.PLAID, connection_id="item_1"),
        cursor="cursor-one",
    )
    db.commit()

    for source in sync_module.linked_sources(db, ada):
        sync_module.sync_source(db, ada, source)

    by_item = {s.item_id: s for s in stub_plaid_source.built}
    assert by_item["item_1"].cursors == ["cursor-one"]
    assert by_item["item_2"].cursors == [None]

    assert repository.cursor_for(
        db, ada, ConnectionRef(provider=Provider.PLAID, connection_id="item_1")
    ) == "cursor-after-item_1"


def test_the_loop_only_visits_the_named_users_connections(db, ada, bob,
                                                          stub_plaid_source):
    import sync as sync_module

    _link(db, ada, "item_1", ACCESS_TOKEN)
    _link(db, bob, "item_2", OTHER_ACCESS_TOKEN)

    assert [s.item_id for s in sync_module.linked_sources(db, ada)] == ["item_1"]
    assert [s.item_id for s in sync_module.linked_sources(db, bob)] == ["item_2"]


def test_a_connection_without_a_token_is_not_a_source(db, ada,
                                                      stub_plaid_source):
    """A fake or CSV connection row has no access token, and there is nothing
    to build a PlaidSource from. Skipped rather than half-built."""
    import sync as sync_module

    repository.save_connection(
        db, ada, ConnectionRef(provider=Provider.FAKE, connection_id="fixture")
    )
    db.commit()

    assert sync_module.linked_sources(db, ada) == []
    assert len(repository.connections_for(db, ada)) == 1


def test_the_cli_path_is_unchanged_when_nothing_is_linked(db, monkeypatch,
                                                          stub_plaid_source):
    """The local CLI user has no linked connections, so `sources_for` is still
    exactly the DATA_SOURCE-selected adapter -- one source, as before."""
    import sync as sync_module

    monkeypatch.setenv("DATA_SOURCE", "fake")
    user_id = users.ensure_local_user(db)

    sources = sync_module.sources_for(db, user_id)

    assert len(sources) == 1
    assert sources[0].connection().connection_id == "fixture"
    assert stub_plaid_source.built == []


def test_linked_connections_are_synced_alongside_the_configured_source(
    db, ada, monkeypatch, stub_plaid_source
):
    import sync as sync_module

    monkeypatch.setenv("DATA_SOURCE", "fake")
    _link(db, ada, "item_1", ACCESS_TOKEN)

    sources = sync_module.sources_for(db, ada)

    assert sorted(s.connection().connection_id for s in sources) == [
        "fixture", "item_1",
    ]


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def test_the_api_refuses_to_start_without_an_encryption_key(monkeypatch):
    """Fail at startup, not at the first exchange. Without the key the app
    runs perfectly until somebody links an account, and the failure lands on a
    user mid-handshake rather than on whoever deployed it."""
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)

    with pytest.raises(crypto.EncryptionKeyMissing):
        with TestClient(app):
            pass


def test_the_api_starts_when_the_key_is_present(client):
    assert client.get("/health").status_code == 200
