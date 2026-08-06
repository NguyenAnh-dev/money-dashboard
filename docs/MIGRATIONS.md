# Step 7 — migrations, and a connection id that names its owner

`557 passed, 1 skipped`, `GATE PASSED`, **render byte-identical**.

Schema **v7**. A v6 database is now migrated on open rather than refused.

Two changes, and they arrived together on purpose: the second one is a data
rewrite, so it needed the first to exist before it could ship.

---

## 1. Why "delete the file and re-sync" stopped being an answer

It was a fine answer for six versions, and the reason was never that migrations
are hard. It was that **everything in the file was reproducible**. `sync.py`
reads a fixture, a CSV or a bank; whatever the database held, running it again
rebuilt it. A schema bump cost a few seconds.

6.3 ended that. `connections.access_token` holds a Fernet-encrypted Plaid grant,
and there is no command that regenerates one: Link is a browser flow with a human
in it. So from 6.3 onward, deleting the file costs **every linked bank**, and the
message telling you to do it was quietly asking for something it could not give
back.

The version check itself was never wrong. What was wrong is that it had exactly
one response to an old file.

---

## 2. The shape

```
store.open_db(path)
  └─ connect()        pragmas, foreign_keys ON
  └─ init_schema()
       ├─ is_new = _database_is_empty(conn)      asked BEFORE the DDL
       ├─ executescript(SCHEMA)                  CREATE TABLE IF NOT EXISTS
       ├─ stamp SCHEMA_VERSION                   new files only
       ├─ migrations.upgrade(conn, SCHEMA_VERSION)
       └─ _require_supported_version(conn)
```

The order is the whole design and each position is load-bearing:

- **`is_new` first.** After the DDL runs, every database looks like it has always
  had these tables. This is the only moment the difference is visible, and it is
  what stops a version being *guessed* onto a file with history. Unchanged from
  5.3.
- **DDL second.** `CREATE TABLE IF NOT EXISTS` adds tables that are missing and
  changes nothing about tables that exist. It is therefore safe to run before a
  migration and useless as one — pointing it at a v6 `transactions` table does
  not add a v7 column, it silently does nothing. That gap is the entire reason
  `SCHEMA_VERSION` exists.
- **Migrations third**, because they need the tables the DDL may just have
  created, and because the version check must judge what they *left*.
- **The version check last**, and its two halves now mean different things —
  see §5.

`init_schema` returns the list of versions it passed through (`[7]`, or `[]` for
the overwhelmingly common no-op), so a caller can report it. Nothing prints it
yet.

---

## 3. The registry

```python
MIGRATIONS = {6: to_v7}
```

Keyed on the version you are coming **from**, so `upgrade` walks
`range(found, target)` and applies each in turn. A step that returns without
raising is stamped as complete, one version at a time — a file interrupted
between two steps re-opens at the version it actually reached, not at the one it
was heading for.

**A bump without a step is refused, not skipped.** `upgrade` checks the whole
range up front and raises `MigrationFailed` naming the gap. The failure it
prevents is the quiet one: stamping v8 onto a file that never got the v8 change,
after which every SELECT works and the first write dies on a column that is not
there. `test_the_registry_reaches_the_current_schema_version` catches it one step
earlier still — at the moment `SCHEMA_VERSION` is edited, in a test rather than
in somebody's database.

### v6 is the floor, and it is a floor rather than a gap

`EARLIEST_SUPPORTED_VERSION = 6`. A file below it is refused with a message
saying so.

Writing v1→v6 retroactively was considered and rejected: **no database outside
this repository was ever written at those versions**, so it is five migrations
nobody will ever run, untestable against any real file, and rotting from the day
they are written. There is also a clean argument that the cost of refusing them
is zero — a pre-v6 file cannot hold a linked Plaid connection, because
`access_token` did not exist until 6.3, so a re-sync of one loses nothing that
needs a browser and a human to rebuild. That is exactly the property that made
the old policy acceptable, and it still holds below the floor.

---

## 4. Foreign keys are OFF while a step runs

This is the part to be careful with, and it is not optional for v6→v7.

The step rewrites `accounts.id`, which `transactions.account_id` references. With
foreign keys on there is no ordering that works: updating the parent breaks the
children, and updating the children first points them at a row that does not
exist yet. SQLite's own guidance for this class of change is to disable
enforcement, do the rewrite, and verify afterwards. `ON UPDATE CASCADE` would be
the alternative and it is not available retroactively — the constraint is baked
into the table definition, so adding it would itself be a table rebuild.

So `_apply` does exactly three things around the step:

```python
conn.execute("PRAGMA foreign_keys = OFF")
try:
    with conn:                       # one transaction, all or nothing
        step(conn)
        _require_referential_integrity(conn, version)   # PRAGMA foreign_key_check
        conn.execute("UPDATE meta SET schema_version = ? ...")
finally:
    conn.execute("PRAGMA foreign_keys = ON")
```

Three details, each with a test:

- **`foreign_key_check` runs INSIDE the transaction**, before the version stamp.
  Checking after the commit would report the damage rather than prevent it.
  `test_a_step_that_orphans_a_row_is_rolled_back` drives a deliberately
  half-written step through it.
- **The pragma is restored in `finally`**, because it is per *connection* and
  this is the same connection the caller then syncs with. Left off, that
  connection would accept orphaned writes for the rest of its life.
  `test_foreign_keys_are_enforced_again_after_a_migration`.
- **The version stamp is inside the transaction**, so a step that raises leaves
  both the rows and the version untouched and the next open retries from the
  same place. `test_a_step_that_raises_leaves_the_database_untouched`.

**A note for the next migration:** this is safe for DML. Python's `sqlite3` in
legacy autocommit mode does not open a transaction for DDL, so an `ALTER TABLE`
inside a step is *not* covered by that rollback on versions before 3.12. A step
that needs DDL should do it as a table rebuild inside explicit `BEGIN`/`COMMIT`,
or the file has to be treated as unrecoverable mid-step.

---

## 5. What the version check means now

Its two halves have stopped being symmetrical.

**Newer than us: refused, unchanged, and still the more dangerous direction.**
The extra columns are invisible to every SELECT we write, so the app would read
the file happily, render plausible numbers, and drop them on the next write.
There is no migrating backwards. The message says *upgrade the code*, and
explicitly says do **not** delete the file.

**Older than us: now unreachable.** By the time `_require_supported_version`
runs, migrations have either walked the file forward or raised. If it still finds
an older version, that is a step reporting success without stamping what it
reached — a bug in `core/migrations.py`, not a problem with the database, and the
message says so rather than telling a user to delete something.

**No `meta` row at all: still refused, and migrations cannot help.** They start
from a version and this file does not state one. Inventing one is the thing
`init_schema` has refused to do since 5.3.

---

## 6. v6 → v7: the connection id gains its owner

### The problem

`make_id` produces `provider:connection_id:provider_id` with **no user segment**,
so the connection id is the only thing separating one person's rows from
another's. Two of the three adapters hardcoded theirs as a module constant —
`fake.CONNECTION_ID = "fixture"`, `csv_source.CONNECTION_ID = "bank_export"` —
which is the same string for everybody.

The second user to sync the fixture therefore minted **identical** account ids to
the first, and `repository.ensure_connections` refused the write. That refusal
was correct: the alternative is an upsert overwriting somebody else's rows while
doing exactly what an upsert is for. But it meant the practical demo was
single-user on anything but Plaid, and the error a second user got came from a
constant in an adapter with no visible connection to the account they had just
created.

Plaid never had this problem. An Item id is unique per user per institution by
construction.

### The fix

`FakeSource` and `CsvSource` take a `connection_id` and default it to their
constant. `data_source.get_source(user_id)` scopes it when an owner is named:

```python
scoped_connection_id("fixture", user_id)  ->  "fixture:<uuid>"
```

`sync.py` passes the user. `get_accounts()` / `get_all_transactions()` and a bare
`FakeSource()` do not, and produce exactly the ids they always did — that is the
inspection path and the conformance suite's, and neither is about ownership.

**Plaid is deliberately not routed through it.** Scoping an Item id would rekey
every linked account to buy a property it already has, on the one connection a
re-sync cannot rebuild.

### Why scope the connection id rather than add a user segment to `make_id`

Both were on the table. The user segment is arguably the more honest fix, and it
**rekeys every row ever minted, for every provider including Plaid**, plus every
id in the baseline. Scoping the connection id keeps `make_id` untouched, leaves
ids at three segments, leaves Plaid alone entirely, and rekeys only the two
sources that had the collision. The user segment remains available if a later
need justifies it.

The escaping already handled the rest: a connection id containing a `:` is
escaped to `%3A` by `_escape_segment`, so the id stays three segments and stays
injective. `test_a_scoped_connection_id_names_its_owner` pins that.

### What the migration does

For every `connections` row with provider `fake` or `csv`:

1. compute `new = scoped_connection_id(old, user_id)`, skipping rows already
   scoped;
2. refuse if that connection already exists — two rows would have to merge, and
   an upsert picking a winner is precisely the silent overwrite being removed;
3. rewrite `connections.connection_id`;
4. rewrite each account's `id` and `connection_id`, via `make_id` rather than
   string surgery;
5. rewrite each transaction's `id`, `connection_id`, **and `account_id`**, mapped
   through the accounts rekeyed in step 4.

Step 5 is the one that matters. Rekey the accounts and leave the children
pointing at the old strings and every read's join returns nothing — a dashboard
that renders perfectly with no accounts and no money on it. That is what
`foreign_key_check` is standing behind, and what
`test_every_transaction_still_resolves_to_its_account` states directly.

`test_a_migrated_database_is_indistinguishable_from_a_fresh_sync` is the
strongest available statement: the migrated ids are **exactly** the ids the
current sync path writes, not merely self-consistent ones. If those drifted, the
next sync would insert a second copy of every row beside the migrated one.

---

## 7. The render diff that was predicted, and did not happen

The prediction going in was that rekeying every fixture id would move the render.
It does not, and the reasoning is worth keeping because it is the reasoning that
would have to change for it ever to be false.

**No id is printed.** The dashboard shows institutions, account names, merchants,
categories, amounts and dates. Nothing renders a key.

**Ordering is the real exposure, and it survives.** `TRANSACTION_SELECT` orders
by `t.date, t.id`, and `main.py` then re-sorts by date descending with Python's
*stable* sort — so within one date, the order on screen is the order the ids
sorted in. That is a genuine dependency of the rendered text on the id strings,
and it is the only one.

It holds because **every id in a connection gains the same new prefix**. The
comparison between two ids of one connection is decided by the segment after the
prefix, which is minted from row content (`adapters/fingerprint.py`) and does not
depend on the connection at all. Relative order is therefore invariant, and the
"Recent Transactions" table cannot reorder or change which rows fall inside the
limit of 10.

Two ways that could stop being true, neither of which is in play here:

- a database holding **two connections of the same provider** with different
  scoping, where the prefixes differ and interleaving could change — that is not
  what a rekey produces, since it moves the whole connection at once;
- anything that starts **displaying** an id, at which point the baseline pins it
  and every rekey is a deliberate `--update`.

Verified rather than argued: `GATE PASSED` with the baseline untouched, and
`test_the_dashboard_renders_identically_either_way` renders a migrated database
against a freshly synced one, with an assertion that the output is not the empty
screen so it cannot pass trivially.

---

## 8. What this does not fix

- **`sync.py` still writes as `LOCAL_USER_ID`.** A signed-up API user's dashboard
  is still empty, because there is no `POST /sync` and the CLI has no notion of
  logging in. Scoping was the blocker underneath that; it is no longer the
  blocker, but the command still has to grow a way to say who it is syncing as.
- **The CSV account identity is still `DEFAULT_ACCOUNT`** for everybody, so two
  users importing statements both get an account called "Bank Export". Their ids
  no longer collide, which was the correctness problem; the naming is cosmetic
  and unaddressed.
- **No `ENCRYPTION_KEY` rotation**, which is the other migration-shaped hole and
  a genuinely different one: it needs both keys present at once and touches
  ciphertext rather than schema.
- **A migration has no dry-run and no backup step.** It rewrites the file in
  place, having verified referential integrity inside the transaction. For a
  database holding live Plaid grants, a copy taken beforehand is still the only
  thing that survives a bug in a step.
