"""
tests/test_api.py
-----------------
Step 6.2: the HTTP surface. Driven through FastAPI's TestClient, which runs the
app in-process over httpx -- no server, no port, no --reload.

The two that matter, and the reason this file exists at all:

  * every data endpoint is 401 without a session, and
  * user A's request never returns user B's rows.

Everything else is detail around those two.

THERE IS NO SHARED AUTHENTICATED CLIENT FIXTURE, deliberately. A client that
arrives already carrying a cookie is exactly how an endpoint that forgot its
dependency passes its tests. Every request here goes through `_as(client,
token)`, which clears the jar first, so each call states the identity it speaks
as -- including `_as(client)` for nobody at all.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api.app import app
from api.deps import get_db
from core import repository, sessions, store, users
from core.models import (
    Account,
    AccountType,
    Provider,
    SourceRef,
    Transaction,
    TransactionKind,
    make_id,
)

PASSWORD = "correct-horse-battery-staple"

#: Named for readable parametrize ids. Completeness is not trusted to this
#: list -- test_every_route_is_either_public_or_401 derives it from the app.
DATA_ENDPOINTS = ("/accounts", "/transactions", "/spending", "/recurring")

PUBLIC_ROUTES = {
    "/health", "/auth/signup", "/auth/login", "/auth/logout",
    "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
}


def _api_routes(routes):
    """FastAPI 0.141 keeps included routers nested rather than flattening them
    into app.routes, so a plain iteration finds only /health."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _api_routes(route.original_router.routes)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "api.db")


@pytest.fixture
def db(db_path):
    conn = store.open_db(db_path)
    yield conn
    conn.close()


@pytest.fixture
def client(db, db_path):
    """The app, wired to the test database rather than to DB_PATH.

    The override opens a connection PER REQUEST, exactly as `get_db` does in
    production. Handing every request the fixture's own connection fails
    outright: TestClient runs the app in a worker thread, and a sqlite3
    connection belongs to the thread that created it.

    Overriding the dependency rather than setting DB_PATH also keeps the real
    money.db out of reach even if a test forgets to isolate itself.
    """
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


def _as(client, token: str | None = None) -> TestClient:
    client.cookies.clear()
    if token is not None:
        client.cookies.set("session", token)
    return client


def _account(connection_id, provider_id, **kwargs) -> Account:
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


def _transaction(account, provider_id, **kwargs) -> Transaction:
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


def _user(db, email="ada@example.com"):
    """A user without paying argon2 -- these tests are not about hashing."""
    return users.create_user_with_hash(db, email, "argon2:stand-in")


def _seed(db, user_id, tag, merchant="Blue Bottle", cents=575):
    account = _account(f"{tag}_conn", f"acc_{tag}", name=f"{tag} Checking")
    txn = _transaction(account, f"txn_{tag}", merchant=merchant,
                       amount_cents=cents)
    repository.save(db, user_id, [account], [txn])
    return account, txn


def _seed_fixture(db, user_id):
    from adapters.fake import FakeSource

    source = FakeSource()
    repository.save(db, user_id, source.fetch_accounts(),
                    source.fetch_transactions().added)


def _signup(client, email, password=PASSWORD):
    response = _as(client).post(
        "/auth/signup", json={"email": email, "password": password}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ---------------------------------------------------------------------------
# THE test: nothing is readable without a session
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", DATA_ENDPOINTS)
def test_every_data_endpoint_is_401_without_a_session(client, path):
    response = _as(client).get(path)

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


@pytest.mark.parametrize("path", DATA_ENDPOINTS)
def test_every_data_endpoint_is_401_with_a_junk_cookie(client, path):
    for cookie in ("", "not-a-token", sessions.new_token()):
        response = _as(client, cookie).get(path)
        assert response.status_code == 401, (path, cookie)


@pytest.mark.parametrize("path", DATA_ENDPOINTS)
def test_every_data_endpoint_is_401_with_an_expired_session(client, db, path):
    user_id = _user(db)
    token = sessions.create_session(
        db, user_id,
        now=datetime.now() - timedelta(days=30),
        lifetime=timedelta(hours=1),
    )

    assert _as(client, token).get(path).status_code == 401


@pytest.mark.parametrize("path", DATA_ENDPOINTS)
def test_a_logged_out_session_stops_working_immediately(client, db, path):
    """The whole argument for server-side sessions, asserted over HTTP. With a
    JWT this token would keep working until it expired."""
    token = sessions.create_session(db, _user(db))

    assert _as(client, token).get(path).status_code == 200
    _as(client, token).post("/auth/logout")
    assert _as(client, token).get(path).status_code == 401


def test_every_route_is_either_public_or_401(client):
    """Derived from the app's own routing table, so it cannot rot.

    A new endpoint added without a session dependency fails here until someone
    either adds the dependency or declares the route public on purpose -- which
    is the decision worth forcing. POSTs are walked too, with an empty body: a
    write endpoint that authenticates only after parsing its body would answer
    422 here, which is a leak of a different shape (it tells an anonymous
    caller what the endpoint expects).
    """
    checked = []
    for route in _api_routes(app.routes):
        if route.path in PUBLIC_ROUTES:
            continue
        for method in sorted(route.methods & {"GET", "POST"}):
            checked.append(f"{method} {route.path}")
            send = _as(client).get if method == "GET" else _as(client).post
            response = send(route.path)
            assert response.status_code == 401, (method, route.path,
                                                 response.status_code)

    assert set(checked) == {
        "GET /accounts",
        "GET /transactions",
        "GET /spending",
        "GET /recurring",
        "GET /auth/me",
        "GET /plaid/connections",
        "POST /plaid/link-token",
        "POST /plaid/exchange",
    }


# ---------------------------------------------------------------------------
# THE other test: one user's request never returns another's rows
# ---------------------------------------------------------------------------

def test_a_request_never_returns_another_users_rows(client, db):
    """Two users, the same merchant and identical account shapes, so a missing
    scope shows up as plausible extra money rather than an obviously foreign
    row -- the repository-level test asked again through HTTP, where the user
    id arrives in a cookie instead of an argument."""
    ada = _user(db, "ada@example.com")
    bob = _user(db, "bob@example.com")
    _seed(db, ada, "ada", merchant="Netflix", cents=1599)
    _seed(db, bob, "bob", merchant="Netflix", cents=9999)

    ada_token = sessions.create_session(db, ada)
    bob_token = sessions.create_session(db, bob)

    hers = _as(client, ada_token).get("/accounts").json()
    his = _as(client, bob_token).get("/accounts").json()
    assert len(hers) == len(his) == 1
    assert hers[0]["id"] != his[0]["id"]

    hers = _as(client, ada_token).get("/transactions").json()
    his = _as(client, bob_token).get("/transactions").json()
    assert [t["amount_cents"] for t in hers] == [1599]
    assert [t["amount_cents"] for t in his] == [9999]


def test_the_aggregate_endpoints_are_scoped_too(client, db):
    """Where a leak hides: the category names stay right and only the totals
    are wrong, which reads as a busy month rather than as a bug."""
    ada = _user(db, "ada@example.com")
    bob = _user(db, "bob@example.com")
    _seed(db, ada, "ada", merchant="Netflix", cents=1599)
    _seed(db, bob, "bob", merchant="Netflix", cents=9999)

    hers = _as(client, sessions.create_session(db, ada)).get("/spending")
    his = _as(client, sessions.create_session(db, bob)).get("/spending")

    assert hers.json() == [{"category": "Food & Drink", "amount_cents": 1599}]
    assert his.json() == [{"category": "Food & Drink", "amount_cents": 9999}]


def test_the_cookie_is_the_only_thing_that_decides_whose_data_comes_back(client, db):
    """Ruling out the obvious alternative: there is no user_id query parameter,
    header, or body field that changes the answer."""
    ada = _user(db, "ada@example.com")
    bob = _user(db, "bob@example.com")
    _seed(db, bob, "bob", cents=9999)

    token = sessions.create_session(db, ada)
    for extra in ({"params": {"user_id": bob}}, {"headers": {"X-User-Id": bob}}):
        response = _as(client, token).get("/transactions", **extra)
        assert response.status_code == 200
        assert response.json() == []


def test_an_unauthenticated_request_is_401_even_when_data_exists(client, db):
    """A 401 that only happens to be right because the database is empty would
    pass every test above."""
    _seed(db, _user(db), "ada")

    assert _as(client).get("/accounts").status_code == 401
    assert _as(client).get("/transactions").status_code == 401


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------

def test_signup_creates_a_user_and_logs_them_in(client, db):
    response = _as(client).post(
        "/auth/signup",
        json={"email": "Ada@Example.com", "password": PASSWORD},
    )

    assert response.status_code == 201
    assert response.json()["email"] == "ada@example.com"
    assert users.user_exists(db, response.json()["id"])
    assert client.cookies.get("session")


def test_signup_never_echoes_the_password_back(client):
    response = _as(client).post(
        "/auth/signup", json={"email": "ada@example.com", "password": PASSWORD}
    )
    assert PASSWORD not in response.text


def test_a_duplicate_email_is_409_not_500(client):
    """`EmailAlreadyRegistered` exists so this is a decision the endpoint
    makes, rather than a raw sqlite3.IntegrityError reaching the handler and
    becoming an unhandled 500."""
    _signup(client, "ada@example.com")

    response = _as(client).post(
        "/auth/signup", json={"email": "ADA@example.com", "password": PASSWORD}
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "That email is already registered"


def test_a_short_password_is_rejected_before_it_is_hashed(client):
    response = _as(client).post(
        "/auth/signup", json={"email": "ada@example.com", "password": "short"}
    )
    assert response.status_code == 422


def test_a_malformed_email_is_rejected(client):
    response = _as(client).post(
        "/auth/signup", json={"email": "not-an-email", "password": PASSWORD}
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Login and logout
# ---------------------------------------------------------------------------

def test_login_sets_a_session_cookie(client, db):
    user_id = _signup(client, "ada@example.com")

    response = _as(client).post(
        "/auth/login", json={"email": "ada@example.com", "password": PASSWORD}
    )

    assert response.status_code == 200
    assert response.json()["id"] == user_id
    assert sessions.resolve_session(db, client.cookies["session"]) == user_id


def test_login_accepts_the_email_in_any_casing(client):
    _signup(client, "ada@example.com")

    response = _as(client).post(
        "/auth/login", json={"email": "ADA@Example.com", "password": PASSWORD}
    )
    assert response.status_code == 200


def test_the_wrong_password_is_401(client):
    _signup(client, "ada@example.com")

    response = _as(client).post(
        "/auth/login",
        json={"email": "ada@example.com", "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert "session" not in response.headers.get("set-cookie", "")


def test_login_does_not_say_which_half_was_wrong(client):
    """The endpoint's half of the constant-time work in core/users.py.
    Distinguishing 'no such account' from 'wrong password' in the response body
    hands over the same fact the dummy verification exists to protect."""
    _signup(client, "ada@example.com")

    unknown = _as(client).post(
        "/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    wrong = _as(client).post(
        "/auth/login",
        json={"email": "ada@example.com", "password": "wrong-password"},
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_the_local_user_cannot_log_in_over_http(client, db):
    """The `!` hash, asked through the front door."""
    users.ensure_local_user(db)

    for guess in ("!", "", PASSWORD, "local"):
        response = _as(client).post(
            "/auth/login",
            json={"email": users.LOCAL_USER_EMAIL, "password": guess},
        )
        assert response.status_code in (401, 422), guess


def test_logout_deletes_the_session_row(client, db):
    user_id = _signup(client, "ada@example.com")
    token = client.cookies["session"]
    assert sessions.active_sessions(db, user_id) == 1

    response = client.post("/auth/logout")

    assert response.status_code == 204
    assert sessions.active_sessions(db, user_id) == 0
    assert db.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0
    assert sessions.resolve_session(db, token) is None


def test_logout_clears_the_cookie(client):
    _signup(client, "ada@example.com")
    response = client.post("/auth/logout")

    assert "session=" in response.headers["set-cookie"]
    assert not client.cookies.get("session")


def test_logout_without_a_session_is_not_an_error(client):
    """Idempotent on purpose: a client clearing a cookie it no longer has a row
    for should not get a 401 telling it to log in so that it can log out."""
    assert _as(client).post("/auth/logout").status_code == 204


def test_logout_only_ends_the_session_that_asked(client, db):
    """Two devices. Signing out of one must not sign out the other."""
    user_id = _user(db)
    laptop = sessions.create_session(db, user_id)
    phone = sessions.create_session(db, user_id)

    _as(client, laptop).post("/auth/logout")

    assert sessions.resolve_session(db, laptop) is None
    assert sessions.resolve_session(db, phone) == user_id


def test_me_reports_the_session_holder(client, db):
    user_id = _signup(client, "ada@example.com")

    response = client.get("/auth/me")

    assert response.status_code == 200
    assert response.json() == {"id": user_id, "email": "ada@example.com"}


def test_me_is_401_without_a_session(client):
    assert _as(client).get("/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# The cookie itself
# ---------------------------------------------------------------------------

def test_the_session_cookie_is_httponly_and_samesite(client):
    """HttpOnly keeps the token out of `document.cookie`, so an XSS bug cannot
    read it. SameSite=Lax is what stops a cross-site POST riding the session,
    and is the only CSRF defence this step ships."""
    response = _as(client).post(
        "/auth/signup", json={"email": "ada@example.com", "password": PASSWORD}
    )

    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "path=/" in header


def test_the_secure_flag_is_configurable_and_defaults_off(monkeypatch):
    """Off by default so local http development works, ON in production via
    COOKIE_SECURE. A Secure cookie is never sent over http, so defaulting it to
    True makes `uvicorn --reload` on localhost look like a broken login for
    reasons nothing on screen explains.

    This is the setting to change before anything is deployed, which is why it
    is a test and not a comment.
    """
    from api import settings

    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    assert settings.cookie_secure() is False

    for truthy in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("COOKIE_SECURE", truthy)
        assert settings.cookie_secure() is True, truthy

    monkeypatch.setenv("COOKIE_SECURE", "0")
    assert settings.cookie_secure() is False


def test_the_cookie_expires_with_the_session(client):
    """A cookie outliving its row is a client that thinks it is logged in until
    the next request says otherwise."""
    from api import settings

    response = _as(client).post(
        "/auth/signup", json={"email": "ada@example.com", "password": PASSWORD}
    )

    assert f"max-age={settings.max_age_seconds()}" in response.headers[
        "set-cookie"
    ].lower()
    assert settings.max_age_seconds() == int(
        sessions.SESSION_LIFETIME.total_seconds()
    )


def test_the_raw_token_is_still_not_in_the_database(client, db):
    """The cookie carries the token; the table carries its SHA-256. Asserted
    here too, because the HTTP layer is where a 'just store it, it is easier'
    change would land."""
    _signup(client, "ada@example.com")
    token = client.cookies["session"]

    stored = db.execute("SELECT * FROM sessions").fetchone()
    assert token not in tuple(stored)
    assert stored["token_hash"] == sessions.token_hash(token)


# ---------------------------------------------------------------------------
# What a response body is allowed to contain
# ---------------------------------------------------------------------------

def test_an_account_response_carries_no_provenance(client, db):
    """AccountOut is a deliberate subset. `source` is internal plumbing --
    shipping it to a browser exports an implementation detail into a contract
    clients would then depend on."""
    user_id = _user(db)
    _seed(db, user_id, "ada")

    body = _as(client, sessions.create_session(db, user_id)).get(
        "/accounts"
    ).json()[0]

    assert set(body) == {
        "id", "name", "institution", "type", "balance_cents", "currency",
        "mask", "available_cents", "as_of",
    }
    for leaked in ("source", "raw", "provider", "connection_id", "user_id"):
        assert leaked not in body


def test_a_transaction_response_never_carries_raw(client, db):
    """`raw` is the one that matters: on a Plaid row it is kilobytes of
    provider payload, and it is the field most likely to hold something a
    client should never have seen."""
    user_id = _user(db)
    account = _account("ada_conn", "acc_ada")
    txn = _transaction(
        account, "txn_ada",
        raw={"account_owner": "Ada Lovelace", "secret": "do-not-ship"},
    )
    repository.save(db, user_id, [account], [txn])

    response = _as(client, sessions.create_session(db, user_id)).get(
        "/transactions"
    )

    assert "do-not-ship" not in response.text
    assert set(response.json()[0]) == {
        "id", "account_id", "date", "name", "merchant", "amount_cents",
        "category", "currency", "kind", "pending",
    }


def test_money_crosses_the_wire_as_integer_cents(client, db):
    """Invariant 1, at the boundary where it is easiest to lose: JSON has one
    number type, so a float here arrives as a float and every client inherits
    the rounding bug. Formatting is the client's job -- no dollar signs."""
    user_id = _user(db)
    _seed(db, user_id, "ada", cents=1234)
    token = sessions.create_session(db, user_id)

    accounts = _as(client, token).get("/accounts")
    txn = _as(client, token).get("/transactions").json()[0]

    assert isinstance(accounts.json()[0]["balance_cents"], int)
    assert isinstance(txn["amount_cents"], int)
    assert txn["amount_cents"] == 1234
    assert "$" not in accounts.text


def test_a_date_crosses_the_wire_as_an_iso_date_not_a_datetime(client, db):
    user_id = _user(db)
    _seed(db, user_id, "ada")

    txn = _as(client, sessions.create_session(db, user_id)).get(
        "/transactions"
    ).json()[0]

    assert txn["date"] == "2026-07-27"


def test_an_enum_crosses_the_wire_as_its_value(client, db):
    user_id = _user(db)
    account = _account("ada_conn", "acc_ada", type=AccountType.CREDIT,
                       balance_cents=-1_000)
    repository.save(db, user_id, [account], [
        _transaction(account, "txn_ada", kind=TransactionKind.TRANSFER)
    ])
    token = sessions.create_session(db, user_id)

    assert _as(client, token).get("/accounts").json()[0]["type"] == "credit"
    assert _as(client, token).get(
        "/transactions"
    ).json()[0]["kind"] == "transfer"


# ---------------------------------------------------------------------------
# The API and the CLI answer the same questions the same way
# ---------------------------------------------------------------------------

def test_the_api_is_a_projection_of_the_same_functions_the_cli_renders(client, db):
    """Not a second implementation. If these ever disagree, one of them has
    grown its own filtering."""
    from insights import find_recurring, spending_by_category

    user_id = _user(db)
    _seed_fixture(db, user_id)
    token = sessions.create_session(db, user_id)

    accounts = repository.load_accounts(db, user_id)
    transactions = repository.load_transactions(db, user_id)

    assert len(_as(client, token).get("/accounts").json()) == len(accounts)
    assert len(_as(client, token).get("/transactions").json()) == len(transactions)
    assert len(_as(client, token).get("/recurring").json()) == len(
        find_recurring(transactions)
    )
    assert len(_as(client, token).get("/spending").json()) == len(
        spending_by_category(transactions)
    )


def test_the_recurring_endpoint_reports_a_price_change(client, db):
    """The fixture's T-Mobile rise, over HTTP. `delta_cents` and `direction`
    are derived PROPERTIES on the dataclass, and pydantic reads fields -- so
    they have to be spelled out in the schema or they vanish silently."""
    user_id = _user(db)
    _seed_fixture(db, user_id)

    charges = _as(client, sessions.create_session(db, user_id)).get(
        "/recurring"
    ).json()

    by_merchant = {c["merchant"]: c for c in charges}
    assert by_merchant["T-Mobile"]["direction"] == "up"
    assert by_merchant["T-Mobile"]["delta_cents"] == 746
    assert by_merchant["Spotify"]["direction"] is None
    assert by_merchant["Spotify"]["delta_cents"] is None


def test_no_transfer_reaches_the_spending_endpoint(client, db):
    """4.8's rule, still holding one layer out. The fixture's Amex payment is a
    transfer, and counting it would put $1634.49 of money that was never spent
    into a table that claims to show spending."""
    user_id = _user(db)
    _seed_fixture(db, user_id)
    token = sessions.create_session(db, user_id)

    spending = _as(client, token).get("/spending").json()
    recurring = _as(client, token).get("/recurring").json()

    assert "Transfer" not in {row["category"] for row in spending}
    assert "Amex Payment" not in {row["merchant"] for row in recurring}
    assert len(recurring) == 4


# ---------------------------------------------------------------------------
# The CLI is untouched
# ---------------------------------------------------------------------------

def test_the_cli_does_not_import_fastapi():
    """Step 5.1's split, extended. `sync.py` and `main.py` must keep working
    with fastapi uninstalled, so neither may reach it -- directly or through
    core/.

    A subprocess, for the same reason the argon2 check is one: fastapi is
    already imported in this process.
    """
    import subprocess
    import sys

    script = (
        "import sys, main, sync;"
        "leaked = sorted(m for m in sys.modules "
        "  if m.split('.')[0] in {'fastapi', 'starlette', 'uvicorn', 'pydantic'});"
        "assert not leaked, leaked;"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=str(store.PROJECT_ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_the_api_never_creates_a_user_of_its_own(client, db):
    """`LOCAL_USER_ID` is CLI scaffolding. An API request must not summon it
    into existence -- an endpoint that called `ensure_local_user` would hand
    every visitor the same shared account."""
    _as(client).get("/accounts")
    _as(client).post(
        "/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )

    assert db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0
