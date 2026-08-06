"""
tests/test_auth.py
------------------
Step 6.1: signup, login, sessions. No HTTP -- these are the functions 6.2's
endpoints will call, tested at the level they actually live at.

Nothing here renders, so the baseline gate is unaffected by every line of it.
That is the point of testing this layer directly: an auth bug does not show up
on screen, and there is no diff that would catch one.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from core import passwords, sessions, store, users

PASSWORD = "correct-horse-battery-staple"
OTHER_PASSWORD = "Tr0ub4dor&3-and-then-some"


@pytest.fixture
def db(tmp_path):
    conn = store.open_db(str(tmp_path / "auth.db"))
    yield conn
    conn.close()


@pytest.fixture
def ada(db):
    return users.create_user(db, "ada@example.com", PASSWORD)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def test_a_password_hash_is_argon2id_not_a_general_purpose_digest():
    """THE decision of the step, readable straight off the stored string.

    A general-purpose hash is engineered to be fast, which is exactly the
    property a password hash must not have: speed is what makes an offline
    attack against a stolen database cheap. `hashlib.sha256` would produce a
    64-character hex string here; argon2 produces a PHC string naming the
    algorithm and the cost parameters it was produced with.
    """
    stored = passwords.hash_password(PASSWORD)

    assert stored.startswith("$argon2id$")
    assert passwords.is_argon2_hash(stored)
    assert len(stored) != 64


def test_the_same_password_hashes_differently_every_time():
    """The salt, observed rather than asserted about. Two users with the same
    password must not have the same row, or a leaked database reveals which
    accounts share one."""
    first = passwords.hash_password(PASSWORD)
    second = passwords.hash_password(PASSWORD)

    assert first != second
    assert passwords.verify_password(first, PASSWORD)
    assert passwords.verify_password(second, PASSWORD)


def test_verification_rejects_the_wrong_password():
    stored = passwords.hash_password(PASSWORD)
    assert passwords.verify_password(stored, OTHER_PASSWORD) is False
    assert passwords.verify_password(stored, PASSWORD + " ") is False
    assert passwords.verify_password(stored, "") is False


def test_a_hash_that_is_not_a_hash_returns_false_rather_than_raising():
    """argon2 raises InvalidHashError on a string it cannot parse -- including
    the local user's `!`. A verifier that let that propagate would turn a
    disabled account into a 500 instead of a failed login."""
    for garbage in (users.UNUSABLE_PASSWORD_HASH, "", "sha256:abc", "!x"):
        assert passwords.verify_password(garbage, PASSWORD) is False


def test_a_too_short_password_is_refused_at_the_hash():
    with pytest.raises(ValueError, match="at least"):
        passwords.hash_password("short")


def test_rendering_does_not_import_argon2(tmp_path, monkeypatch):
    """The same argument requirements.txt makes about the Plaid SDK, pointed at
    the KDF: argon2-cffi is a C extension with its own transitive deps, and
    `main.py` needs exactly one constant out of core/users.py.

    Run in a subprocess because argon2 is already imported in this one -- an
    in-process check would pass no matter what main.py did.
    """
    import subprocess
    import sys

    script = (
        "import sys, main;"
        "assert 'argon2' not in sys.modules, sorted("
        "  m for m in sys.modules if 'argon2' in m);"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=str(store.PROJECT_ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_hashing_is_deliberately_slow():
    """Stated as a test because it is the one property that looks like a
    defect. If this ever runs in under a millisecond, the cost parameters have
    been lost and every stored hash is cheap to attack offline."""
    started = datetime.now()
    passwords.hash_password(PASSWORD)
    elapsed = datetime.now() - started

    assert elapsed > timedelta(milliseconds=1)


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------

def test_signup_stores_a_hash_and_never_the_password(db, ada):
    stored = db.execute(
        "SELECT password_hash FROM users WHERE id = ?", (ada,)
    ).fetchone()["password_hash"]

    assert PASSWORD not in stored
    assert passwords.is_argon2_hash(stored)
    assert passwords.verify_password(stored, PASSWORD)


def test_signup_normalizes_the_email_before_storing_it(db):
    users.create_user(db, "  Ada@Example.COM ", PASSWORD)

    stored = db.execute("SELECT email FROM users").fetchone()["email"]
    assert stored == "ada@example.com"


def test_a_duplicate_email_is_a_clean_error_not_an_integrity_error(db, ada):
    """The caller is an HTTP endpoint at 6.2, and it has to tell a user 'that
    email is taken' without either leaking SQL or catching sqlite3 exceptions
    three layers above the database."""
    with pytest.raises(users.EmailAlreadyRegistered) as caught:
        users.create_user(db, "ada@example.com", OTHER_PASSWORD)

    assert caught.value.email == "ada@example.com"
    assert isinstance(caught.value, ValueError)
    assert not isinstance(caught.value, sqlite3.IntegrityError)


def test_a_duplicate_signup_leaves_the_original_row_untouched(db, ada):
    with pytest.raises(users.EmailAlreadyRegistered):
        users.create_user(db, "ADA@example.com", OTHER_PASSWORD)

    assert db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 1
    assert users.authenticate(db, "ada@example.com", PASSWORD) == ada
    assert users.authenticate(db, "ada@example.com", OTHER_PASSWORD) is None


def test_an_integrity_error_that_is_not_a_duplicate_email_still_raises(db):
    """The except clause is narrow on purpose. Swallowing every IntegrityError
    as 'email taken' would report the wrong cause for a malformed id, a missing
    column, or any constraint added later."""
    with pytest.raises(sqlite3.IntegrityError):
        users.create_user_with_hash(
            db, "x@example.com", "hash", user_id="not-a-uuid"
        )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def test_a_correct_password_returns_the_user_id(db, ada):
    assert users.authenticate(db, "ada@example.com", PASSWORD) == ada


def test_login_accepts_the_email_in_any_casing(db, ada):
    assert users.authenticate(db, "  ADA@Example.COM ", PASSWORD) == ada


def test_the_wrong_password_returns_none(db, ada):
    assert users.authenticate(db, "ada@example.com", OTHER_PASSWORD) is None


def test_an_unknown_email_returns_none(db, ada):
    assert users.authenticate(db, "nobody@example.com", PASSWORD) is None


def test_login_returns_none_rather_than_raising_on_a_junk_email(db):
    assert users.authenticate(db, "   ", PASSWORD) is None


def test_the_local_user_cannot_be_authenticated_with_any_password(db):
    """The `!` convention, now load-bearing rather than decorative.

    No argon2 output starts with `!`, so there is no password that verifies
    against it -- but that is only true because `authenticate` refuses the row
    before verifying. This asserts the outcome over a range of guesses,
    including the sentinel itself.
    """
    users.ensure_local_user(db)

    for guess in (
        users.UNUSABLE_PASSWORD_HASH,
        "",
        PASSWORD,
        "local",
        "!",
        "password",
    ):
        assert users.authenticate(db, users.LOCAL_USER_EMAIL, guess) is None


def test_an_unknown_email_still_costs_a_verification(db, ada, monkeypatch):
    """The timing side channel, closed and pinned.

    Returning early on a missing row makes an unknown email measurably faster
    than a known one with a wrong password -- which turns the login form into
    an oracle for 'is this person a customer of yours'. For a finance app that
    is the answer worth protecting even before the password is.

    Asserted by counting the verification rather than by timing it: a wall
    clock assertion on a CI runner is a flake generator.
    """
    users.ensure_local_user(db)

    calls = []
    monkeypatch.setattr(
        passwords, "waste_a_verification", lambda *a, **k: calls.append(1)
    )

    users.authenticate(db, "nobody@example.com", PASSWORD)
    assert len(calls) == 1

    users.authenticate(db, "   ", PASSWORD)
    assert len(calls) == 2

    users.authenticate(db, users.LOCAL_USER_EMAIL, PASSWORD)
    assert len(calls) == 3, (
        "a row with an unusable hash must cost the same as a missing one -- "
        "otherwise 'disabled account' is distinguishable from 'no account'"
    )


def test_a_real_verification_is_not_wasted_on_top(db, ada, monkeypatch):
    """The other half: a known email must NOT pay twice, or the mitigation
    becomes its own signal in the opposite direction."""
    calls = []
    monkeypatch.setattr(
        passwords, "waste_a_verification", lambda *a, **k: calls.append(1)
    )

    users.authenticate(db, "ada@example.com", OTHER_PASSWORD)
    assert calls == []


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def test_a_valid_session_resolves_to_the_user_that_created_it(db, ada):
    token = sessions.create_session(db, ada)
    assert sessions.resolve_session(db, token) == ada


def test_an_expired_session_resolves_to_nothing(db, ada):
    issued = datetime(2026, 8, 1, 9, 0, 0)
    token = sessions.create_session(
        db, ada, now=issued, lifetime=timedelta(hours=1)
    )

    assert sessions.resolve_session(db, token, now=issued) == ada
    assert sessions.resolve_session(
        db, token, now=issued + timedelta(minutes=59)
    ) == ada
    assert sessions.resolve_session(
        db, token, now=issued + timedelta(hours=1)
    ) is None
    assert sessions.resolve_session(
        db, token, now=issued + timedelta(days=400)
    ) is None


def test_expiry_is_a_boundary_not_a_range(db, ada):
    """`<=` rather than `<`, stated so nobody 'fixes' it. A session whose
    expiry is exactly now has expired."""
    issued = datetime(2026, 8, 1, 9, 0, 0)
    expires = issued + timedelta(hours=1)
    token = sessions.create_session(db, ada, now=issued,
                                    lifetime=timedelta(hours=1))

    assert sessions.resolve_session(
        db, token, now=expires - timedelta(seconds=1)
    ) == ada
    assert sessions.resolve_session(db, token, now=expires) is None


def test_an_expired_row_is_still_in_the_table_until_it_is_purged(db, ada):
    """Expiry is enforced on READ, not by the row vanishing on its own. A test
    that only checked the count would pass against a resolver that never
    checked the date at all."""
    issued = datetime(2026, 8, 1, 9, 0)
    later = issued + timedelta(days=2)
    sessions.create_session(db, ada, now=issued, lifetime=timedelta(hours=1))

    assert db.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1
    assert sessions.active_sessions(db, ada, now=later) == 0

    assert sessions.purge_expired(db, now=later) == 1
    assert db.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0


def test_purging_leaves_live_sessions_alone(db, ada):
    now = datetime(2026, 8, 1, 9, 0)
    dead = sessions.create_session(db, ada, now=now, lifetime=timedelta(hours=1))
    live = sessions.create_session(db, ada, now=now, lifetime=timedelta(days=7))

    assert sessions.purge_expired(db, now=now + timedelta(days=1)) == 1
    assert sessions.resolve_session(db, dead, now=now + timedelta(days=1)) is None
    assert sessions.resolve_session(db, live, now=now + timedelta(days=1)) == ada


def test_the_raw_token_is_never_stored(db, ada):
    """The reason this table holds a `token_hash` column and not a `token` one.

    A session token is a bearer credential: whoever holds it IS the user until
    it expires. Stored raw, a database leak hands the attacker every live
    session directly -- no cracking required, and rotating passwords does not
    help. Hashing costs one SHA-256 per request, and unlike a password a
    128-bit random token has nothing to brute-force, so a fast hash is the
    right one here.
    """
    token = sessions.create_session(db, ada)

    stored = db.execute("SELECT * FROM sessions").fetchone()
    assert token not in tuple(stored)
    assert stored["token_hash"] == sessions.token_hash(token)
    assert len(stored["token_hash"]) == 64

    columns = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
    assert "token" not in columns


def test_a_token_holder_cannot_derive_another_users_session(db, ada):
    """Tokens are random, not derived from the user. Otherwise one leaked
    session plus a user id would mint the rest."""
    other = users.create_user_with_hash(db, "b@example.com", "hash")

    mine = sessions.create_session(db, ada)
    theirs = sessions.create_session(db, other)

    assert mine != theirs
    assert sessions.resolve_session(db, mine) == ada
    assert sessions.resolve_session(db, theirs) == other


def test_two_sessions_for_one_user_are_independent(db, ada):
    """Two devices. Logging out of one must not log out of the other."""
    laptop = sessions.create_session(db, ada)
    phone = sessions.create_session(db, ada)

    assert laptop != phone
    assert sessions.delete_session(db, laptop) is True

    assert sessions.resolve_session(db, laptop) is None
    assert sessions.resolve_session(db, phone) == ada


def test_logout_actually_deletes_the_row(db, ada):
    """THE argument for server-side sessions over a stateless JWT. A JWT stays
    valid until it expires no matter what the server thinks, so 'log out' can
    only mean 'the client agreed to forget it'. Here the credential stops
    existing."""
    token = sessions.create_session(db, ada)
    sessions.delete_session(db, token)

    assert db.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0
    assert sessions.resolve_session(db, token) is None


def test_logging_out_twice_is_not_an_error(db, ada):
    token = sessions.create_session(db, ada)

    assert sessions.delete_session(db, token) is True
    assert sessions.delete_session(db, token) is False


def test_every_session_for_a_user_can_be_revoked_at_once(db, ada):
    """The other thing a JWT cannot do: 'sign me out everywhere' after a
    password change or a lost phone."""
    other = users.create_user_with_hash(db, "b@example.com", "hash")
    mine = [sessions.create_session(db, ada) for _ in range(3)]
    theirs = sessions.create_session(db, other)

    assert sessions.delete_sessions_for_user(db, ada) == 3

    for token in mine:
        assert sessions.resolve_session(db, token) is None
    assert sessions.resolve_session(db, theirs) == other


def test_an_unknown_token_resolves_to_nothing(db, ada):
    sessions.create_session(db, ada)

    for junk in ("", "not-a-token", sessions.new_token(), "0" * 64):
        assert sessions.resolve_session(db, junk) is None


def test_a_session_token_has_real_entropy():
    minted = {sessions.new_token() for _ in range(1000)}

    assert len(minted) == 1000
    assert all(len(t) >= 43 for t in minted)
    assert sessions.TOKEN_BYTES >= 32


def test_a_session_cannot_belong_to_a_user_that_does_not_exist(db):
    with pytest.raises(sqlite3.IntegrityError):
        sessions.create_session(db, users.new_user_id())


def test_a_session_that_expires_when_it_is_issued_is_refused(db, ada):
    for lifetime in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError, match="lifetime must be positive"):
            sessions.create_session(db, ada, lifetime=lifetime)


def test_the_default_lifetime_is_finite(db, ada):
    """A session with no expiry is a password that never changes. The value is
    policy and can move; that it exists is not."""
    issued = datetime(2026, 8, 1, 9, 0)
    token = sessions.create_session(db, ada, now=issued)

    assert sessions.SESSION_LIFETIME > timedelta(0)
    assert sessions.resolve_session(
        db, token, now=issued + sessions.SESSION_LIFETIME
    ) is None


# ---------------------------------------------------------------------------
# Login and session, composed the way 6.2 will compose them
# ---------------------------------------------------------------------------

def test_the_login_flow_end_to_end(db, ada):
    user_id = users.authenticate(db, "Ada@Example.com", PASSWORD)
    assert user_id == ada

    token = sessions.create_session(db, user_id)
    assert sessions.resolve_session(db, token) == ada

    sessions.delete_session(db, token)
    assert sessions.resolve_session(db, token) is None


def test_a_failed_login_never_reaches_a_session(db, ada):
    """Stated because the composition is the easy thing to get wrong: an
    endpoint that mints a session from `authenticate`'s return value without
    checking it for None would issue a session for user `None`."""
    user_id = users.authenticate(db, "ada@example.com", OTHER_PASSWORD)
    assert user_id is None

    with pytest.raises((sqlite3.IntegrityError, ValueError, TypeError)):
        sessions.create_session(db, user_id)


def test_a_session_resolves_to_a_user_id_the_repository_can_scope_by(db, ada):
    """The seam between Step 6 and 6.1: what a session resolves TO is exactly what
    every repository function takes as its second argument."""
    from core import repository

    token = sessions.create_session(db, ada)
    user_id = sessions.resolve_session(db, token)

    assert repository.load_accounts(db, user_id) == []
    assert repository.load_transactions(db, user_id) == []
