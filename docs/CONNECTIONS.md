# Step 5.8, part one — `connections` and incremental fetch

Items **1 and 2** of 5.8. Deletion (item 3) and pending→settled reconciliation
(item 4) are deliberately **not** here — see *What is deliberately still open*.

`338 passed, 1 skipped`, `GATE PASSED`, render byte-identical.

---

## The split, and why here

Incremental fetching is one idea: *give the provider a resume token and it tells
you only what changed*. Deletion and reconciliation are a different idea: *given
a description of what changed, decide what the database should now look like*.
The first is about the fetch, the second about the write, and each has its own
failure modes.

They also have very different risk profiles. Items 1–2 change **how much** you
fetch; items 3–4 change **what happens to rows you already have** — including
deleting them. Landing those on top of an unproven cursor would mean debugging
both at once.

---

## Predictions, made before writing any code

| Predicted | Actual |
|---|---|
| Zero render diff | Zero. `GATE PASSED`, byte-identical |
| Schema v3, existing v2 databases refused | Confirmed, with the right message |
| `SourceRef` stops round-tripping exactly | Confirmed — one test rewritten, deliberately |
| 8-ish tests fail on `fetched_at` / cursor API | 8 failed, all in those two categories |

The one worth dwelling on is the third, below.

---

## Item 1 — the `connections` table

```sql
CREATE TABLE IF NOT EXISTS connections (
    provider        TEXT NOT NULL,
    connection_id   TEXT NOT NULL,
    cursor          TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (provider, connection_id),
    CHECK (provider IN ('fake', 'plaid', 'csv')),
    CHECK (length(connection_id) > 0)
);
```

**Keyed on `(provider, connection_id)`, not a surrogate id.** That pair *is* the
identity — it is literally the first two segments of every id the connection
mints. A synthetic primary key would be a second name for something that already
has one, plus a lookup to translate between them.

**This fixes a real defect introduced at 5.7**, not just an aesthetic one. The
5.7 scaffolding put one `sync_cursor` column in a one-row `meta` table, so two
Plaid Items shared a single slot: syncing either would resume the other from the
wrong point and silently skip transactions. Two tests now hold that shut —
`test_two_connections_keep_separate_cursors` and
`test_a_fake_sync_cannot_clobber_a_plaid_cursor`.

### `fetched_at` moved, and `SourceRef` stopped round-tripping

`core/models.py` said this from the beginning:

> `fetched_at` — changes on EVERY sync … Moves to the connection record once
> there is a database.

So the column is **gone from `accounts` and `transactions`.** It was one value
per fetch, identical across every row of that fetch — 76 copies of one timestamp
— and because the upsert refreshes every column, changing that one value rewrote
all 76 rows.

The consequence, predicted in advance and accepted:

```python
account = FakeSource().fetch_accounts()[0]   # source.fetched_at is None
repository.save(conn, [account], [], connection=connection)
repository.load_accounts(conn)[0].source.fetched_at   # -> the sync time
```

**An `Account` no longer reads back byte-identical to the one that went in.**
That is correct rather than regrettable: `fetched_at` describes *when this data
was fetched*, which is a property of the connection, not of the row. The adapter
leaving it `None` was always a placeholder — it has no way to know when the
sync that carries it will happen.

`test_source_ref_is_reassembled_from_its_columns_plus_the_connection` states the
new rule: three columns from the row, the fourth from `connections`.

**Read path cost:** `load_accounts` / `load_transactions` each read the whole
`connections` table once per call and index it in memory. It has one row per
linked source — three, realistically — so this is cheaper than a join and much
cheaper than a per-row lookup.

**`save` truncates to whole seconds** (`replace(microsecond=0)`), so
`connections.fetched_at` and `meta.last_synced_at` describe the same moment at
the same precision. They disagreed on first implementation — one carried
microseconds, the other did not — which is exactly the "two formattings of one
timestamp" trap `SaveResult.synced_at` exists to avoid.

---

## Item 2 — incremental fetch

### `connection()` joins the adapter contract

```python
def connection(self) -> ConnectionRef: ...
def fetch_transactions(self, cursor: str | None = None) -> SyncResult: ...
```

**Why a new method rather than deriving the connection from the records.** The
cursor is stored under `(provider, connection_id)`, so `sync.py` must know which
connection it is about to fetch **before** it fetches — that is the lookup key.
Deriving it from returned records is too late by definition, and deriving it
from `DATA_SOURCE` would name the provider but never the Item.

Three conformance tests keep it honest:

- `test_connection_identifies_the_source_before_it_fetches` — it is a
  `ConnectionRef` with a real provider and a non-empty id.
- `test_connection_matches_the_provenance_of_every_record` — the connection a
  source *names* and the one its records *claim* are the same. If they drift,
  the cursor is filed under a key nothing reads back, and every sync silently
  refetches everything.
- `test_connection_is_stable_across_calls` — an identity that changed between
  calls would file each sync's cursor under a fresh key, which reads exactly
  like never having synced.

### `cursor` on every adapter, honoured by one

Fake and CSV accept a cursor and ignore it. That is not a stub: both hand back
complete current state on every fetch and have nothing to resume from.
`test_a_cursorless_source_ignores_a_cursor_it_is_given` pins the specific
failure it prevents — a source that returned *nothing* because it was handed a
token it did not understand would look identical to a source with no new data.

Plaid passes it straight into the first `/transactions/sync` request. The
pagination loop already reassigned `cursor` per page, so the parameter simply
becomes the starting value.

### `sync.py` reads the cursor before fetching

```python
connection = source.connection()
conn = store.open_db()
result = source.fetch_transactions(repository.cursor_for(conn, connection))
accounts = source.fetch_accounts()
```

Note the ordering change: the database is now opened **before** the fetch,
because the cursor lives in it. Transactions are still fetched before accounts —
that is the orphan window closed at 5.7, and it still applies.

**`save_connection` uses `COALESCE(excluded.cursor, connections.cursor)`.** A
fetch that returns no cursor must not erase a working one, because the damage
stays invisible until the next sync silently refetches from the beginning of
time.

---

## The trap this introduces

**`data_source.get_transactions()` is now a fragment for Plaid, if anyone ever
passes it a cursor.** It deliberately does not — it calls
`fetch_transactions()` with no argument, so it still replays everything — but
the meaning of `added` has changed underneath it: it is now "new to us", not
"every transaction".

`check_baseline.check_structure()` is exactly such a caller. It is safe today
only because it is pinned to fake, which has no cursor. Worth knowing before
anything else starts calling it.

---

## What is deliberately still open

- **`removed` still arrives and is still ignored.** Item 3. Plaid now reports
  removals *and* the cursor that makes them incremental, so the deletion policy
  finally has the input it was waiting for.
- **Pending → settled reconciliation.** Item 4. `raw.pending_transaction_id` is
  stored and read by nothing.
- **No migration path.** v2 databases are refused with "delete the file and
  re-sync", verified. Three schema versions in three steps is fine while this is
  a learning project with a disposable database; it stops being fine the day the
  database holds something a re-sync cannot rebuild.
- **The first incremental Plaid sync is untested against the live API.** The
  recorded fixture cannot exercise resumption — it has no cursor and no second
  state to resume into. `test_a_stored_cursor_is_sent_on_the_first_request` and
  `test_a_resumed_fetch_still_follows_pagination` cover the adapter's half
  against a stub client; the round trip through a real Item is unproven, and
  that is the risk to know about before trusting an incremental sync with real
  data.
