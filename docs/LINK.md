# Step 6.3 — Plaid Link, and encrypted access tokens

`528 passed, 1 skipped`, `GATE PASSED`, **render byte-identical**.

Schema **v6** — one nullable column, `connections.access_token`. v5 databases
are refused with the usual message.

**Verified against the real Plaid sandbox**, not only against stubs: signup →
`/plaid/link-token` → a sandbox `public_token` → `/plaid/exchange` → a stored
Item → `sync` → 12 accounts and 48 transactions served over `/accounts`, with
the plaintext token absent from the database file.

Not in this step: **the browser half.** Link needs a page running Plaid's JS
SDK, and that is the frontend phase. Until then the handshake is driven with
`/sandbox/public_token/create`, which is what the smoke test above does.

---

## 1. The column

```sql
access_token    TEXT,
CHECK (access_token IS NULL OR access_token LIKE 'gAAAAA%'),
```

One column, because Fernet output is a single self-describing string — version,
timestamp, IV, ciphertext and HMAC are all inside it. Nothing else to store.

**NULL for every source except Plaid**, and NULL rather than `''` because "this
concept does not apply" and "the token is the empty string" are different facts,
and only one of them is true of a CSV import.

**The CHECK is the last line of defence**, and it is honest about what it can
do: it cannot verify ciphertext, but it does catch the one mistake worth
catching — a raw `access-sandbox-…` written straight into the column by a hand
query or a future code path that forgot to encrypt.
`test_the_schema_refuses_a_plaintext_token` holds it.

The column is on `connections` and nowhere else, which follows from Step 6: a
token belongs to a linked institution, and a linked institution belongs to a
user through the chain that already exists.

---

## 2. `core/crypto.py`

Fernet, key from `ENCRYPTION_KEY`.

**Why Fernet rather than raw AES.** It is *authenticated* (AES-128-CBC +
HMAC-SHA256), it picks the IV itself, and it has no mode to choose wrongly.
A tampered ciphertext raises instead of decrypting to a corrupted token that
would then be sent to Plaid —
`test_a_tampered_ciphertext_is_rejected_rather_than_returning_garbage`.

**Encrypting twice gives different ciphertext**, because the IV is fresh per
call. That matters beyond textbook correctness: identical ciphertexts would
leak which two users had linked the same institution, without either token
being readable.

**The key is read per call, not cached at import** — the same rule as `DB_PATH`
and `DATA_SOURCE`, and for the same reason: a key frozen at import means a test
that changes it is talking to nobody. The `Fernet` instance *is* cached, keyed
on the string it was built from, so rotating the variable rebuilds it.

**The `cryptography` import is deferred into the functions**, following the
argon2 and plaid-python precedent.

### Failing loudly, and the three different failures

| situation | error | why it is its own message |
|---|---|---|
| variable unset | `EncryptionKeyMissing` | tells you how to generate one |
| variable malformed | `EncryptionKeyInvalid` | a truncated paste; "not set" would send you looking for the wrong thing |
| valid key, wrong key | `EncryptionKeyInvalid`, "rotated" | the rows exist and do not decrypt — a very different problem from a broken config |

**The API refuses to start without the key**, via the FastAPI `lifespan` hook —
not at first use. Checked at first use instead, the server would come up
perfectly, serve the dashboard, and fail on somebody's *first link attempt*,
which puts the error in front of a user instead of whoever deployed it.
`test_the_api_refuses_to_start_without_an_encryption_key` asserts the app will
not even enter its context.

**The CLI does not require the key**, and that is deliberate: `sync.py` with
`DATA_SOURCE=fake` touches no token, and demanding a Fernet key to render the
fixture would be security theatre. Verified by running it with the variable
unset.

**Losing the key is not recoverable.** Every stored token becomes permanently
unreadable and every account has to be re-linked. `.env.example` says so in
those words, next to the generator one-liner:

```
./venv/Scripts/python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## 3. The repository functions

Same shape as everything since Step 6 — `user_id` first, required, positional.

```python
save_access_token(conn, user_id, connection, access_token)   # encrypts
access_token_for(conn, user_id, connection) -> str | None    # decrypts
linked_connections(conn, user_id) -> [(ConnectionRef, token)]
connections_for(conn, user_id) -> [ConnectionRef]
owner_of(conn, connection) -> user_id | None
```

**The token is encrypted on write, decrypted on read, and crosses no other
boundary.** It is not on `SourceRef`, not in any schema in `api/schemas.py`,
and not in any response body.

**`owner_of` is deliberately NOT user-scoped**, and it is the only function
here that is not. Its whole job is to answer *who already holds this Item* so
the exchange endpoint can tell "you already linked this" from "somebody else
did" — two situations that must not produce the same response. Scoping it would
make it useless for the only question it exists to answer. It returns a user
id and nothing else; no token, no rows.

**`save_access_token` scopes its UPDATE** and raises when the row belongs to
someone else — otherwise a second user could overwrite the first's token and
point their sync at a different set of credentials.

The strongest test in the file reads the database **as bytes off disk** rather
than through a `SELECT`: the point of encrypting is that somebody who copies
the file gets nothing, and only the file itself can answer that.

---

## 4-5. The endpoints

```
POST /plaid/link-token   -> {"link_token": "link-sandbox-…"}
POST /plaid/exchange     -> 201 {"provider","connection_id","linked"}
GET  /plaid/connections  -> [ … ]
```

All three require a session. All three are in the route-coverage test, which
now walks **POSTs as well as GETs** — a write endpoint that authenticated only
after parsing its body would answer 422 to an anonymous caller, which leaks the
shape of the request rather than the data.

**`client_user_id` is our user id, taken from the cookie.** Never from the
request body: a Link session opened on someone else's behalf would attach the
resulting Item to them. It is a UUID rather than an email in part because it
goes to a third party — one of the reasons §2 of `docs/USERS.md` gives.

**Link asks only for `transactions`.** Asking for more widens the bank's
consent screen for data nothing reads, and Plaid bills per product.

**Exchanging an Item somebody else holds is a 409, not a takeover.** An Item id
*is* the connection id, so permitting it would let one user overwrite another's
token. **Re-linking your own Item is a 201**, because re-running Link issues a
new access token and the old one stops working — the row has to move with it.
Those are different situations and they get different answers.

**`sync.py` is untouched by the API.** There is still no `POST /sync`.

---

## 6. The sync loop

`sync()` returns a **list** of `ConnectionSync(connection, result)` now, one per
source, because a user can have several linked institutions. Before this the
path assumed exactly one, so a second bank was simply never fetched — silently,
since the first still synced and the dashboard still rendered.

The cursors were already per connection in the schema since 5.8. This is what
makes the loop honour that: `test_each_connection_is_handed_its_own_cursor`
gives one Item a stored cursor and asserts the other gets `None`. Handing every
Item the same token would resume one from another's position and skip
transactions with no error.

`sources_for(conn, user_id, include_configured=True)` returns every stored
connection **that has a token**, plus the `DATA_SOURCE`-selected adapter unless
it is already one of them. A connection with no token — fake, CSV — is skipped
rather than half-built; there is nothing to construct a `PlaidSource` from.

For the local CLI user, which has no linked connections, this is exactly one
source and the previous behaviour, byte for byte.

---

## 7. `PlaidSource` already took a token from the caller

Nothing to do. The constructor has taken `access_token` since 4.7 and
`from_env()` was always the classmethod on top — the docstring at the time said
"so the object is testable and two Items can coexist in one process", which is
precisely what this step needed.

What *did* move: the SDK client construction, out of `PlaidSource._api()` and
into a module-level `build_client()`. The two Link calls need a client
**before** any access token exists, which is the shape of the handshake:
`/link/token/create` and `/item/public_token/exchange` are Item-lifecycle calls,
not reads from an Item.

---

## The bug the live run found

`uvicorn api.app:app --reload`, as documented in CLAUDE.md, **never loads
`.env`**. `sync.py` and `main.py` call `load_dotenv()` in their `__main__`
blocks — deliberately, so that importing them does not read config — and
uvicorn imports `api.app` as a module, so nothing runs it.

The symptom was not a config error. `PLAID_CLIENT_ID` reached urllib3 as `None`
and died in `http.client`:

```
TypeError: expected string or bytes-like object, got 'NoneType'
```

— a traceback naming neither Plaid nor the variable, three libraries below the
code that asked. `build_client()` now checks both credentials and says which is
missing *and* why it is likely missing.

**The fix for the command is `--env-file .env`**, which is uvicorn's own way of
doing the job, so config loading stays an entry-point responsibility exactly as
the project's convention says. Documented in CLAUDE.md's command block.

---

## What is deliberately still open

- **The browser half.** Link is a JS SDK and a page; without it there is no way
  for a real user to produce a `public_token`. This is the frontend phase.
- **No `POST /sync`**, unchanged. A linked user's data is fetched by running
  the CLI against their user id, which is not yet a command-line flag.
- **No unlink / `/item/remove`.** Deleting the row would leave the Item live at
  Plaid, still billable and still authorized; doing it properly means calling
  `/item/remove` and handling the case where that call fails after the row is
  gone.
- **No key rotation path.** Rotating `ENCRYPTION_KEY` means decrypting every
  row with the old key and re-encrypting with the new one, inside one
  transaction, with both keys available. The error message tells you what
  happened; there is no command that fixes it.
- **Webhooks are not handled**, so removals and new transactions are only seen
  when a sync runs.
- **`ITEM_LOGIN_REQUIRED` is not handled.** When a bank's credentials go stale
  Plaid returns that error and the answer is Link in update mode — another
  browser flow, so it waits for the frontend too.
