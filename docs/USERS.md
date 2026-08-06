# Step 6 — `users`, and scoping every query to one

`384 passed, 1 skipped`, `GATE PASSED`, render byte-identical.

Schema **v4**. Existing v3 databases are refused with the usual message; there is
still no migration path, so the fix is delete and re-sync.

---

## 1. The schema

```sql
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    created_at      TEXT NOT NULL,

    CHECK (length(id) = 36),
    CHECK (length(email) > 0 AND email = lower(email)),
    CHECK (length(password_hash) > 0),
    CHECK (length(created_at) = 19)
);
```

`user_id` appears **once in the schema**, on `connections`:

```sql
user_id  TEXT NOT NULL REFERENCES users(id)
```

Not on `accounts`, not on `transactions`. Those already belong to a connection,
and a connection belongs to a user, so the chain carries the answer. Copying
`user_id` onto every row would write one value 76 times per sync and then need
keeping consistent with the connection it was copied from — the same
denormalization `fetched_at` was moved *out* of those tables to stop at 5.8.

### The chain is a foreign key, not a convention

Both tables gained one:

```sql
FOREIGN KEY (provider, connection_id)
    REFERENCES connections(provider, connection_id)
```

This is the part that was **not optional**, and it is worth being explicit about
why. Every read now joins through `connections` to filter. An account whose
`connections` row is missing therefore joins to nothing and **disappears from
the dashboard while sitting in the file** — no error, no warning, a net worth
quietly missing a bank. Before this step nothing joined, so a missing
`connections` row cost nothing and could not happen; the join is what turns it
into silent data loss, and the FK is what makes it impossible.

The parent columns already carry a unique index (they are the `connections`
primary key), which is what SQLite requires of a composite FK target.

`test_an_account_cannot_exist_without_its_connection` and
`test_user_id_is_stored_once_per_connection_not_once_per_row` hold both halves.

---

## 2. What a user id is

**A UUIDv4, stored as canonical lowercase text** — 36 characters,
`00000000-0000-4000-8000-000000000001` shaped. Minted by `core/users.py`, never
by the database.

**Why not `INTEGER PRIMARY KEY`**, which is the cheaper and more obvious choice:

- A sequential id is **enumerable**. In a URL or a log line it tells the holder
  how many users exist and what the neighbouring ids are.
- Two databases can never be **merged or moved**: user 1 in one file is a
  different person from user 1 in the other, and nothing in either file says so.
- It cannot be minted **before the INSERT**. A caller that wants to hash a
  password and build a row in one go has to write first and read the id back.

None of those are retrofittable once ids are in circulation, and the cost is 36
bytes on a table with one row per linked source.

**Why v4 and not v7.** v7 is time-ordered and indexes better, and it is also not
in the standard library before 3.14 — adopting it means a dependency, for index
locality on a table that will hold single digits of rows. `uuid.uuid4()` is
free. (v7's other trade, that it leaks a creation time, is not a real objection
here: `created_at` is a column.)

**Why store it as text rather than a 16-byte BLOB.** The file stays readable in
a DB browser, which is the same reason enums are stored as their `.value` and
dates as ISO strings. This schema has consistently traded bytes for legibility.

`test_a_user_id_is_a_uuid4_in_canonical_form` and
`test_a_user_id_is_not_an_enumerable_counter` pin the choice.

---

## 3. Email

Normalized to lowercase on the way in (`users.normalize_email`), then `UNIQUE`
on the exact value.

**Deliberately not `COLLATE NOCASE`**, which would achieve the same thing in one
word. `NOCASE` is *banned* everywhere else in this schema — it is what would
merge `aBc123` and `abc123` into one surviving account — so using it here would
leave the file telling two different stories about what a case-insensitive
column means. Normalizing at the boundary keeps every SQL comparison exact and
puts the one place casing is decided in Python, where the reason can be read.

---

## 4. The local user — scaffolding, and marked as such

```python
LOCAL_USER_ID = "00000000-0000-4000-8000-000000000001"
LOCAL_USER_EMAIL = "local@localhost"
UNUSABLE_PASSWORD_HASH = "!"
```

`sync.py` and `main.py` both need *a* user before auth exists. This is that
user, and three things about it are deliberate:

**The id is a fixed constant, not minted per process.** A fresh `uuid4()` on
each run would file every sync under a new owner, and `main.py` — which looks
rows up by this constant — would report "No data yet" against a full database.
The value is deliberately a *valid* v4 UUID that is also obviously synthetic: it
parses everywhere a real id does, and nobody will mistake it for one.

**The password hash is `!`, and it is not a placeholder that will accidentally
work.** `!` is the Unix convention for a disabled password, and no hash function
produces it, so the scaffolding row cannot be authenticated against once a
verifier exists. `users.is_usable_password_hash` is the check a verifier must
make. Note the distinction the tests draw: an **empty** hash is refused
(`ValueError`) because it means a caller forgot to hash; `!` is accepted because
it means a caller meant it.

**Rendering never creates it.** `sync.py` calls `ensure_local_user`; `main.py`
reads `LOCAL_USER_ID` directly. Creating a user as a side effect of a read-only
command is the same class of mistake as `main.py` creating an empty `money.db`,
which 5.6 already went out of its way to prevent.

`test_rendering_reads_the_same_user_sync_writes` pins the one constant the two
commands share. If they drifted, sync would write rows the dashboard could never
find.

---

## 5. `user_id` is a required positional argument

This is the point of the whole step.

```python
repository.load_transactions(conn, user_id)
repository.save(conn, user_id, accounts, transactions)
repository.cursor_for(conn, user_id, connection)
repository.delete_transactions(conn, user_id, ids)
```

Every function that touches user-scoped data takes it, **positionally and
without a default**. Both alternatives were considered and both are worse in the
same way:

- **A default of `LOCAL_USER_ID`** makes a forgotten argument work perfectly
  until the day there are two users, and then silently return the wrong
  person's money.
- **`user_id=None` meaning "everything"** makes a forgotten argument a
  full-table read that returns plausible data belonging to somebody else.

A required argument turns both into an immediate `TypeError` at the call site.
`test_forgetting_the_user_id_is_a_type_error` states that as a property rather
than as a docstring.

Every read joins:

```sql
SELECT t.<cols> FROM accounts t
JOIN connections c
  ON c.provider = t.provider AND c.connection_id = t.connection_id
WHERE c.user_id = ?
```

**Deletes are scoped by the same join**, and that one is load-bearing rather
than tidy. `DELETE FROM transactions WHERE id = ?` needs only an id, and an id
carries no user segment — so unscoped it would happily delete another user's
row, succeed, and report success. `delete_transactions` now resolves the id list
against the *user's* rows first and deletes only what that returned; anything
else comes back as `unmatched`, which is the existing warning channel.
`test_one_user_cannot_delete_another_users_transaction` covers it.

### The isolation tests use OVERLAPPING merchants, deliberately

`test_a_query_scoped_to_one_user_cannot_return_the_others_rows` seeds both users
at the same three merchants — Netflix, Chipotle, Amazon — at different amounts,
and asserts on **amounts** rather than on ids.

Distinct merchants would have made the test easy and nearly worthless: a leak
would show up as an obviously foreign row, which is the failure mode nobody
needs help spotting. Overlapping names produce the failure that hides — the
*right* merchants, the *right* category names, at doubled totals that read as a
busy month. `test_an_unscoped_read_would_be_visible_in_the_spending_table` asks
the same question of `spending_by_category`, which is a sum over whatever it is
handed and so cannot defend itself; the scoping has to have already happened.
`test_the_net_worth_of_one_user_excludes_the_others_accounts` covers balances,
with two accounts identical in name and institution and distinguishable only by
owner.

All three were verified to fail against a deliberately unscoped read (`WHERE
c.user_id = ? OR 1=1`), which is the only evidence that a passing scoping test
is testing anything at all.

### The one function that is NOT scoped, on purpose

`repository.last_synced_at` still reads the single `meta` row, which is global.
It is left that way rather than given a `user_id` parameter it would ignore — a
parameter that claims filtering it does not do is worse than an honest global.
The correct per-user answer is `MAX(connections.fetched_at)` for that user, and
`connections.fetched_at` already holds it; wiring the footer to it is a render
change with its own baseline diff, so it is **deferred**, not overlooked.

---

## 6. A connection belongs to exactly one user

`connections` keeps its `(provider, connection_id)` primary key. `user_id` is
**not** part of it, and this is the sharpest constraint the step introduces.

An id is `provider:connection_id:provider_id` and carries **no user segment**.
So if two users could each hold `fake:fixture`, their accounts would mint
*identical ids* — `fake:fixture:acc_1` for both — and the second user's sync
would `ON CONFLICT DO UPDATE` straight over the first user's rows. Not a
collision that surfaces as an error: an upsert, doing exactly what it was
designed to do, on data belonging to somebody else.

Making `user_id` part of the key would *permit* that arrangement in the
`connections` table while leaving the collision fully intact one table over.
Keeping the global key makes it impossible, and
`repository.ensure_connections` raises when a sync tries to write a connection
another user owns.

**The live consequence at Step 6, stated plainly:** a second user **could not
sync the fake source**, because `FakeSource.CONNECTION_ID` was the constant
`"fixture"` for everybody. That was the correct signal rather than a bug in the
fixture — it was the id namespace saying it was not yet multi-user. Real
multi-user needed one of:

- connection ids minted per user (a real Plaid Item id already is, per
  enrollment — this is only a problem for sources that hardcode one), or
- a user segment in `make_id`, which rekeys **every row ever minted** and is
  therefore a much larger decision than it looks.

### Resolved at Step 7 — the first option

`FakeSource` and `CsvSource` now take a `connection_id`, and
`data_source.get_source(user_id)` hands them
`scoped_connection_id(CONNECTION_ID, user_id)` → `"fixture:<uuid>"`. `sync.py`
supplies the user; `get_accounts()` and a bare `FakeSource()` do not, and are
unchanged. **Plaid is deliberately not scoped**: an Item id is already unique per
user per institution, and rekeying it would cost the one connection a re-sync
cannot rebuild.

`make_id` did not change, and that is the point of choosing this option — the
user segment lives *inside* the connection id, escaped to `%3A`, so ids stay
three segments and every existing Plaid row keeps its key. Existing databases are
rekeyed by the v6→v7 migration; see `docs/MIGRATIONS.md` §6.

**The constraint in this section is unchanged and still enforced.** A connection
still belongs to exactly one user and `ensure_connections` still raises on a
cross-user write —`test_a_connection_belongs_to_exactly_one_user` still passes.
What changed is that the fixture no longer *hands* two users the same connection
to collide over. `test_two_users_can_each_sync_the_fixture` is the other half.

---

## 7. `save` writes the connection first

The write order inside `save` is now:

1. `connections` — from the explicit `ConnectionRef` when there is one
2. `accounts`
3. `transactions`
4. deletes, then the `meta` stamp

Connections moved to the front for the same reason accounts precede
transactions: the foreign key. It was last before, when nothing referenced it.

`save` is also called **without** a `ConnectionRef` in a good deal of the suite
and by the baseline gate, so the connection rows are derived from the records'
own `SourceRef` (`ensure_connections`) rather than required as an argument.
Making the parameter mandatory would have been the other option; deriving it
means no call site can produce an orphaned account by omission, which is the
failure the FK exists to prevent, and it keeps the explicit parameter for what
it is actually for — the cursor and `fetched_at`.

---

## 8. What is deliberately still open

- **There is no authentication.** No login, no session, no password hashing —
  `password_hash` is a column with a `NOT NULL` constraint and one sentinel
  value in it. Choosing the KDF (argon2id) is its own step.
- **No user can be created except by calling `users.create_user` from a
  script.** There is no signup path, which is fine while `LOCAL_USER_ID` is the
  only user any command reaches for.
- **`meta.last_synced_at` is global**, per §5.
- ~~**The id namespace is not multi-user**, per §6.~~ Resolved at Step 7 by
  scoping the connection id; see §6 and `docs/MIGRATIONS.md`.
- ~~**No migration path**, still.~~ Added at Step 7, with v6 as the declared
  floor. The prediction in this line held exactly: four schema versions in four
  steps was fine while the database was disposable, and it stopped being fine at
  6.3, the first version to hold something a re-sync cannot rebuild.
