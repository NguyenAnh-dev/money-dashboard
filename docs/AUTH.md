# Step 6.2 — signup, login, sessions

`426 passed, 1 skipped`, `GATE PASSED`, **render byte-identical**. Nothing here
appears on screen, which is exactly why it is tested at the function level: an
auth bug produces no diff, so there is no render that would catch one.

Schema **v5** — one new table, `sessions`. v4 databases are refused with the
usual message.

No HTTP. These are the functions 6.3's endpoints will call.

---

## 1. Hashing: `core/passwords.py`

`argon2-cffi==25.1.0`, pinned in `requirements.txt`. **Never `hashlib`** — a
general-purpose digest is engineered to be *fast*, which is the one property a
password hash must not have. Speed is what makes an offline attack against a
stolen database cheap; argon2id is deliberately slow and memory-hard.

Two functions, plus the machinery around them:

```python
hash_password(password) -> str      # "$argon2id$v=19$m=65536,t=3,p=4$..."
verify_password(hash, password) -> bool
```

**`verify_password` returns `False` rather than raising.** argon2 raises
`InvalidHashError` on a string it cannot parse — including the local user's
`"!"` — and a verifier that let that propagate would turn a disabled account
into a 500 instead of a failed login. `VerifyMismatchError`,
`VerificationError` and `InvalidHashError` all collapse to `False`.

**`hash_password` enforces a minimum length** (`MIN_PASSWORD_LENGTH = 8`). That
is policy and can move; a signup path that accepts `""` is a defect regardless.

**`needs_rehash` exists and is unused.** argon2's cost parameters are supposed
to rise as hardware does, and `check_needs_rehash` is how a login learns that a
stored hash was made with the old ones. Wiring it in means re-hashing during
login — which needs the plaintext, which only exists at that moment — and that
belongs with the endpoint that has a response to write it in. Present so the
signal is not lost; deliberately not called yet.

### The argon2 import is lazy, and that is on purpose

`main.py` imports `core.users` for exactly one constant, and `core.users`
imports `core.passwords`. Imported at module scope, argon2-cffi — a **C
extension** with `cffi` and `pycparser` behind it — would be loaded on every
render of a static dashboard.

That is the same argument `requirements.txt` already makes about the Plaid SDK
("none of them should be imported to render a list of tuples"), so it gets the
same treatment: `passwords.hasher()` imports on first use and caches.
`test_rendering_does_not_import_argon2` pins it **in a subprocess**, because
argon2 is already in `sys.modules` in the test process and an in-process check
would pass no matter what `main.py` did.

### `"!"` stopped being decorative

6.1 wrote `UNUSABLE_PASSWORD_HASH = "!"` on the local user on the argument that
no hash function produces it. There was no verifier then, so the claim cost
nothing. Now there is one, and it holds: argon2 output always begins `$argon2`,
so nothing verifies against `!`.

Note the belt-and-braces, though — `authenticate` refuses the row *before*
verifying, via `is_usable_password_hash`. The prefix check is not the only line
of defence, because "no hash function produces this string" is a property of
today's hash function.

---

## 2. Signup: `create_user(conn, email, password)`

Normalizes the email, hashes the password, inserts, returns the id.

**A duplicate email raises `EmailAlreadyRegistered`, not `sqlite3.IntegrityError`.**
The caller at 6.3 is an HTTP handler that has to say "that email is taken"
without catching `sqlite3` exceptions three layers above the database. It
subclasses `ValueError`, and carries `.email`.

**The `except` clause is deliberately narrow** — it matches on `"users.email"`
in the message and re-raises everything else. Swallowing every `IntegrityError`
as "email taken" would report the wrong cause for a malformed user id, a
missing column, or any constraint added later.
`test_an_integrity_error_that_is_not_a_duplicate_email_still_raises` holds that.

**`create_user_with_hash` is the same function taking a ready-made hash.** It is
what `ensure_local_user` uses to write `"!"` — a value `hash_password` would
never produce and `create_user` cannot be talked into — and what the test suite
uses when it needs a user but is not testing hashing, since argon2 costs ~50 ms
a call and most of these tests are about scoping.

**The write is wrapped in `with conn`**, so a rejected signup leaves no row.

---

## 3. Login: `authenticate(conn, email, password)`

Returns the user id, or `None`. Normalizes the email first, so casing and
surrounding whitespace do not decide whether a login works.

### The dummy verification, and why it is not optional

```python
if row is None or not is_usable_password_hash(row["password_hash"]):
    passwords.waste_a_verification(password)
    return None
```

Returning early on a missing row makes an unknown email **measurably faster**
than a known one with the wrong password — argon2 is slow by design, so the
difference is tens of milliseconds and trivially observable over the network.
That turns the login form into an oracle for *is this person a customer of
yours*, which for a finance app is worth protecting before the password is.
Running a throwaway verification against a cached dummy hash makes both paths
cost the same.

Three details:

- **The unusable-hash path wastes one too.** Otherwise "account exists but is
  disabled" is distinguishable from "no account", which leaks the same fact by
  a narrower channel.
- **A successful lookup does NOT pay twice.** A real verification happens
  instead of the dummy one, never on top of it — otherwise the mitigation
  becomes its own signal in the opposite direction.
- **It is tested by counting, not by timing.** A wall-clock assertion on the
  difference between two argon2 calls is a flake generator on shared CI.
  `test_an_unknown_email_still_costs_a_verification` monkeypatches
  `waste_a_verification` and counts the calls.

What this does *not* fix: signup still reveals a registered email by rejecting
it, which is the ordinary trade every product makes, and rate limiting does not
exist yet. Both belong to 6.3, where there is a request to attach them to.

---

## 4. Sessions: server-side tokens, not JWTs

The decision, and the reason, in one line: **logout has to actually work.**

A JWT is valid until it expires no matter what the server thinks, so "log out"
degrades to "the client agreed to forget it" and "sign me out everywhere" is
not expressible at all. The usual fix is a server-side revocation list — at
which point the state is back and the JWT has bought nothing. For a finance app
holding bank balances, revocation is not a nice-to-have.

```sql
CREATE TABLE IF NOT EXISTS sessions (
    token_hash      TEXT PRIMARY KEY NOT NULL,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,

    CHECK (length(token_hash) = 64),
    CHECK (length(created_at) = 19),
    CHECK (length(expires_at) = 19),
    CHECK (expires_at > created_at)
);
```

### The table stores a HASH of the token, never the token

This is a deviation from "a random token, user_id, and expiry" as specified, and
it is worth the extra column name.

A session token is a **bearer credential**: whoever holds it *is* the user until
it expires. Stored raw, a database leak hands an attacker every live session
directly — no cracking, and rotating passwords does not help, because the
sessions outlive the change. Stored hashed, a leak yields nothing usable.

Unlike a password, a fast hash is the **right** choice here: the token is 32
random bytes, so there is no dictionary to run and nothing to brute-force. One
SHA-256 per request is free. `test_the_raw_token_is_never_stored` asserts the
raw token appears in no column, and that no column is named `token`.

### Expiry is enforced on read

`resolve_session` compares `expires_at` against `now`; rows do not remove
themselves. `purge_expired` is the housekeeping call, and it is separate
deliberately — a resolver that deleted as a side effect of a read would turn
every page load into a write.

The comparison is `<=`: a session whose expiry is exactly now **has** expired.
`test_expiry_is_a_boundary_not_a_range` pins the boundary so nobody relaxes it.
`test_an_expired_row_is_still_in_the_table_until_it_is_purged` is the one that
makes the expiry test mean something — without it, a resolver that never
checked the date would pass a test that only counted rows.

### The bug this step's own test caught

```python
expires = issued + (lifetime or SESSION_LIFETIME)      # wrong
expires = issued + (lifetime if lifetime is not None else SESSION_LIFETIME)
```

`timedelta(0)` is **falsy**, so `lifetime or SESSION_LIFETIME` silently
substituted the fourteen-day default for a caller who asked for a zero-length
session — the failure quietly becoming a *longer* session than requested, which
is the wrong direction for a security parameter. Found by
`test_a_session_that_expires_when_it_is_issued_is_refused`, which was written
expecting a `ValueError` and got a working session instead. Same family as
`_require_int_cents` rejecting `bool`: Python's truthiness does not respect
domain meaning.

### The API

```python
create_session(conn, user_id, now=None, lifetime=None) -> token
resolve_session(conn, token, now=None) -> user_id | None
delete_session(conn, token) -> bool          # logout
delete_sessions_for_user(conn, user_id) -> int   # sign out everywhere
purge_expired(conn, now=None) -> int
active_sessions(conn, user_id, now=None) -> int
```

`SESSION_LIFETIME` is 14 days. The value is policy; that it is *finite* is not —
a session with no expiry is a password that never changes.

**What a session resolves to is exactly what every repository function takes as
its second argument.** That is the seam between 6.1 and 6.2, and
`test_a_session_resolves_to_a_user_id_the_repository_can_scope_by` states it,
because it is the whole reason the two steps are in this order.

---

## 5. What is deliberately still open

- **No HTTP, no cookies, no CSRF.** 6.3. Whether the token travels as a cookie
  (`HttpOnly`, `Secure`, `SameSite`) or an `Authorization` header is a decision
  that belongs with the transport, and the CSRF question only exists once it is
  a cookie.
- **No rate limiting and no lockout.** argon2 makes each guess expensive, which
  is not the same as making a million guesses impossible. Needs a request to
  hang off.
- **`needs_rehash` is never called**, per §1.
- **No password reset, no email verification.** Both need a mail path, which
  this project does not have.
- **`sync.py` and `main.py` still use `LOCAL_USER_ID` and ignore all of this.**
  The CLI commands have no session and no way to prompt for one. They stay on
  the scaffolding user until there is an HTTP surface that can log in — the
  ownership plumbing from 6.1 is what makes swapping that a one-line change at
  each call site.
- **The id namespace is still not multi-user** (`docs/USERS.md` §6). Auth now
  makes it possible to *create* a second real user, which makes that ceiling
  reachable rather than theoretical: the second user cannot sync the fake
  source. Resolve it before anyone signs up for real.
