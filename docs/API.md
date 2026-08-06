# Step 6.2 — the HTTP surface

`477 passed, 1 skipped`, `GATE PASSED`, **render byte-identical**. The CLI is
untouched: `sync.py` and `main.py` do not import a line of this, and
`test_the_cli_does_not_import_fastapi` holds them to it in a subprocess.

```
./venv/Scripts/python.exe -m uvicorn api.app:app --reload
```

`/docs` is the interactive OpenAPI page. Every data endpoint is 401 there until
you sign up or log in, because the browser holds the cookie afterwards.

---

## 1. The layout

```
api/
  app.py        the FastAPI instance, and the routers it includes
  deps.py       get_db + current_user_id -- the two dependencies
  settings.py   cookie policy, in one place
  schemas.py    what crosses the wire, and nothing else
  routes/
    auth.py         signup, login, logout, me
    accounts.py     GET /accounts
    transactions.py GET /transactions
    insights.py     GET /spending, GET /recurring
```

Nothing under `core/` or `adapters/` imports `api`, and nothing under `api/`
reaches past `core.repository`, `core.users`, `core.sessions` and `insights`.
The endpoints are a **projection of the functions the dashboard already
renders**, not a second implementation —
`test_the_api_is_a_projection_of_the_same_functions_the_cli_renders` states
that, because the day the two disagree, one of them has grown its own
filtering.

---

## 2. The session dependency, which everything hangs off

```python
def current_user_id(db: Db, session: Cookie = None) -> str:
    if not session:
        raise UNAUTHENTICATED
    user_id = sessions.resolve_session(db, session)
    if user_id is None:
        raise UNAUTHENTICATED
    return user_id
```

Missing cookie, junk cookie, expired session and logged-out session all produce
the **same 401 with the same body**. That is deliberate: distinguishing "no
cookie" from "expired" tells an attacker holding a stolen token whether it was
ever valid.

An endpoint opts in by naming the type:

```python
def list_accounts(user_id: CurrentUser, db: Db):
    return [AccountOut.of(a) for a in repository.load_accounts(db, user_id)]
```

That is the whole of an endpoint. **The user id is not a parameter a client can
supply** — no query string, no header, no body field changes whose data comes
back, and `test_the_cookie_is_the_only_thing_that_decides_whose_data_comes_back`
rules the obvious alternatives out by trying them.

### The route-coverage test cannot rot

`test_every_route_is_either_public_or_401` walks the app's **own routing table**
rather than a hand-written list, and asserts that every GET outside
`PUBLIC_ROUTES` answers 401 unauthenticated. A new endpoint that forgets its
dependency fails there until somebody either adds it or declares the route
public on purpose — which is exactly the decision worth forcing.

Note the walk is not `for route in app.routes`: FastAPI 0.141 keeps included
routers **nested** as `_IncludedRouter` objects rather than flattening them, so
the naive loop finds only `/health` and the test passes while checking almost
nothing. `_api_routes` recurses through `original_router`.

---

## 3. Schemas: what a client is allowed to see

`AccountOut` and `TransactionOut` are deliberate subsets built by an explicit
`.of()` classmethod, not `from_attributes` over the dataclass. The two fields
that must never cross the wire:

- **`source`** (`SourceRef`) is internal plumbing — provider, connection id,
  fetch time. Shipping it exports an implementation detail into a contract
  clients would then depend on, and it is the thing `docs/USERS.md` §6 says has
  to change for real multi-user.
- **`raw`** is the provider's original payload. On a Plaid row that is kilobytes
  of JSON, and it is the field most likely to carry something a client should
  never have seen. `test_a_transaction_response_never_carries_raw` seeds a row
  with `{"secret": "do-not-ship"}` and asserts the string is absent from the
  response, rather than merely checking the key is missing.

Both tests assert the **exact key set**, so a field added to the model does not
silently start shipping.

**Money crosses the wire as integer cents, never formatted.** JSON has one
number type, so a float here would arrive as a float and every client would
inherit the rounding bug invariant 1 exists to prevent. `core/money.py` stays a
rendering concern — there are no dollar signs in any response.

`RecurringChargeOut` spells out `delta_cents` and `direction` explicitly,
because they are `@property` on `RecurringCharge` and **pydantic reads fields**.
Without those two lines the price-change column vanishes from the API while the
CLI keeps showing it — silently, since nothing raises.

---

## 4. Auth routes

| route | success | notes |
|---|---|---|
| `POST /auth/signup` | 201 + cookie | 409 on duplicate email, 422 on a short password or malformed address |
| `POST /auth/login` | 200 + cookie | 401 otherwise, with one message for both halves |
| `POST /auth/logout` | 204 | idempotent; deletes the row and clears the cookie |
| `GET /auth/me` | 200 | 401 without a session |

**Signup logs you in.** One round trip, and the password is sent once rather
than twice. It is a deviation worth naming because the alternative — 201 and
"now go call login" — is also defensible.

**A duplicate email is 409, not 500.** This is what `EmailAlreadyRegistered`
was built for at 6.1: without it a raw `sqlite3.IntegrityError` reaches the
handler and FastAPI turns it into an unhandled 500 with a stack trace.

**Login returns one message for both failures.** `Incorrect email or password`
whether the account is unknown or the password is wrong — the response body's
half of the constant-time work in `core/users.authenticate`. Closing the timing
channel and then printing the answer in the body would be pointless.

**A short password is rejected by the schema** (`Field(min_length=...)` reading
`passwords.MIN_PASSWORD_LENGTH`), so it is a 422 that appears in the OpenAPI
document rather than a 500 from a `ValueError` deep in the KDF.

**Logout is idempotent.** A client clearing a cookie it no longer has a row for
should not get a 401 telling it to log in so that it can log out.

---

## 5. The cookie

`HttpOnly`, `SameSite=Lax`, `Path=/`, `Max-Age` tied to `SESSION_LIFETIME`.

- **HttpOnly** keeps the token out of `document.cookie`, so an XSS bug cannot
  read it.
- **SameSite=Lax** stops a cross-site POST riding the session, and is the only
  CSRF defence this step ships. It covers the common case and is not a complete
  answer — see *still open*.
- **Max-Age matches the row's expiry**, because a cookie outliving its row is a
  client that believes it is logged in until the next request says otherwise.

### `Secure` defaults to OFF, and that is the one to change before deploying

`COOKIE_SECURE=1` turns it on. The default is off because a `Secure` cookie is
never sent over http, so defaulting it to true makes `uvicorn --reload` on
localhost look like a **broken login** for reasons nothing on screen explains —
you log in, get a 200 and a cookie, and every subsequent request is 401.

This is the least comfortable decision in the step: an insecure default in a
finance app. It is a test rather than a comment
(`test_the_secure_flag_is_configurable_and_defaults_off`) so that it is
impossible to change accidentally and impossible to miss when reading the
suite.

**`COOKIE_SECURE=1` is on the deployment checklist in CLAUDE.md — *Before this
is deployed*.** It is written down there rather than only here because nothing
in the app will remind you: unset, logins work, the dashboard renders, and every
session token travels in cleartext over any non-https hop. There is no failure
to notice.

---

## 6. One connection per request

```python
def get_db():
    conn = store.open_db()
    try:
        yield conn
    finally:
        conn.close()
```

A module-level connection would be wrong twice: `sqlite3` connections belong to
the thread that created them, and FastAPI runs sync endpoints in a worker
threadpool.

**That is not a theoretical objection — it is how the first version of the test
suite failed.** The fixture handed every request its own single connection and
37 tests died on `ProgrammingError: SQLite objects created in a thread can only
be used in that same thread`. The override now opens per request, exactly as
production does, which is also what makes the fixture a real test of the real
dependency rather than of a shortcut around it.

**`get_db` will CREATE the database if it is absent**, unlike `main.py`, which
checks first and shows its "no data yet" screen precisely so a read-only command
never creates an empty `money.db` in the wrong directory. A server is the other
case: it has to come up before anything has been synced, and an empty database
is the correct starting state for one.

---

## 7. What is deliberately still open

- **No CSRF token.** `SameSite=Lax` covers cross-site POST, which is the common
  case, but not a same-site attacker or an older browser. A double-submit token
  is the next step if this ever faces the internet.
- **No rate limiting.** Unchanged from 6.1, and now it matters more: `POST
  /auth/login` is a public endpoint and argon2 makes each guess expensive for
  *the server* as much as for the attacker. That cuts both ways — it is also a
  cheap way to exhaust CPU.
- **No CORS configuration**, so no browser on another origin can call this yet.
  That is the safe default; loosening it is a decision to make with a real
  frontend in front of it.
- **No pagination.** `/transactions` returns everything, which is 76 rows from
  the fixture and unbounded in principle.
- **Nothing writes.** There is no `POST /sync` — syncing still belongs to the
  CLI, and giving an HTTP request the power to start a network fetch against a
  bank is a decision with its own failure modes.
- **`sync.py` still writes as `LOCAL_USER_ID`.** So the data a signed-up user
  sees is empty until there is a way to sync as *them* — which needs the id
  namespace fixed first (`docs/USERS.md` §6). Signing up and seeing an empty
  dashboard is currently correct behaviour, and it is the clearest possible
  demonstration that the scoping works.
