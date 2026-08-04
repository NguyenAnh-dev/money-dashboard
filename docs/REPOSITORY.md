# `core/repository.py`, line by line

Step 5.4: the write path. Takes lists of `Account` and `Transaction` objects and
stores them, upserting so a re-sync updates rather than duplicates.

The file itself carries no comments by request, so every "why" lives here. Read
this beside the code.

**Why this is a separate module from `core/store.py`.** `store.py` owns the
*file*: it opens a connection, sets pragmas, creates tables. It deliberately does
not import `Account` or `Transaction` and cannot tell a merchant from a balance.
This module owns the *translation* between rows and model objects — the same
split an adapter makes between a provider payload and the model. Keeping them
apart means a test that wants a bare connection does not drag the model layer in,
and the day storage changes shape, only one of the two files moves.

---

## Imports

```python
from __future__ import annotations
```
Postponed annotation evaluation, so `date | datetime | None` in a signature is a
string at runtime rather than an expression that must resolve. Matches every
other module in the project. (Python 3.10+ is already required for the `X | None`
unions in `core/models.py`.)

```python
import json
import sqlite3
from datetime import date, datetime
from typing import Any, NamedTuple, Sequence
```
Standard library only. `core/models.py`'s "no third-party imports" rule is about
the *model* describing the domain instead of a vendor; `sqlite3` and `json` ship
with Python and describe neither.

```python
from core.models import Account, Transaction
```
Imported for type annotations and nothing else — this module never constructs a
model object, it only reads fields off one. That direction matters: data flows
`models → repository → SQLite`, and if this file ever *built* an `Account`, the
read path would have two places that know how to make one.

---

## The column tuples

```python
ACCOUNT_COLUMNS = ("id", "provider", ..., "as_of")
TRANSACTION_COLUMNS = ("id", "provider", ..., "raw")
```

**Why they exist at all.** Three things must agree exactly: the column list in
the `INSERT`, the `?` placeholder count, and the order of values in the row
tuple. Written out three times by hand, they drift — and the failure mode is
grim. Swap two columns of the same type and SQLite raises nothing: `name` and
`institution` are both `TEXT`, so "Chase Checking" becomes the institution and
"Chase" becomes the account name, and the dashboard renders perfectly with the
fields transposed. Deriving the SQL from one tuple removes the possibility.

**Why tuples, not lists.** They are constants. A module-level list invites
something to append to it, and a mutated column list produces SQL that no longer
matches the row builders below it.

**Why `id` is first.** No functional reason, but it lines up with the schema and
with `_upsert_sql`'s job of excluding exactly one column. Reading the tuple next
to the `CREATE TABLE` should be a visual diff.

---

## `SyncResult`

```python
class SyncResult(NamedTuple):
    accounts: int
    transactions: int
    synced_at: str
```

What `save()` hands back. `sync.py` (5.6) needs to print *something* — "wrote 4
accounts, 76 transactions" — and returning it beats making the caller re-count or
re-query.

**Why `NamedTuple` and not a bare tuple.** `result[1]` at the call site is a
positional read, and positional reads break silently the day a field is added in
the middle. `result.transactions` does not.

**Why `synced_at` is a `str`, not a `datetime`.** It is the exact text written to
`meta.last_synced_at`. Returning the string means the caller can print what is in
the database rather than something formatted a second time from the same source,
which is how two "last synced" values end up disagreeing by a second.

---

## `RawEncoder` and `encode_raw`

```python
class RawEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return super().default(o)
```

**The serializer flagged back at 5.2.** Live Plaid payloads contain `date`
objects, and `json.dumps` raises `TypeError: Object of type date is not JSON
serializable` on them. Without this, every real Plaid sync dies on the first row
— and only the *live* one, because the recorded fixture is JSON on disk and so
carries strings already. That asymmetry is exactly why it needed writing before
anyone tried a live sync.

**`datetime` before `date` in the isinstance tuple.** `datetime` is a *subclass*
of `date`, so checking `date` first would still work here (both branches call
`.isoformat()`, and `datetime.isoformat()` is correctly bound) — but the order
documents that both are intended. Reverse it and add a `date`-only branch later,
and every `datetime` would silently truncate to a day.

**`return super().default(o)` rather than `str(o)`.** The tempting catch-all is
`return str(o)`, which would make this encoder never fail. That is the bug: a
`Decimal`, a `set`, a custom object — anything unexpected — would be stringified
into the archive and be unrecoverable, and nobody would learn Plaid started
sending a new type. Delegating to the base class means anything not explicitly
handled still raises `TypeError`, loudly, in the sync that introduced it.

```python
def encode_raw(raw: dict) -> str:
    return json.dumps(raw, cls=RawEncoder, sort_keys=True)
```

**`sort_keys=True` is not cosmetic.** Python dict order follows insertion order,
so the same payload arriving with keys in a different order would produce
different JSON text — and the upsert would rewrite the row every sync, making
every re-sync look like a change. Sorting makes the encoding a function of the
content alone.

**What this deliberately does not do: round-trip.** A `date` goes out as
`"2026-07-27"` and comes back a `str`. `raw` is an *archive to read fields out
of* — 5.7 wants `pending_transaction_id`, a string, which survives fine — not a
dict to reconstruct and compare against the original. `test_raw_is_lossy_on_
types_by_design` states that so nobody writes an equality round-trip test and is
surprised by it.

---

## `_iso`

```python
def _iso(value: date | datetime | None) -> str | None:
    return None if value is None else value.isoformat()
```

Three nullable columns need this: `SourceRef.fetched_at` (a `datetime`, `None` on
every file-based source), `Account.as_of` (a `date`), and nothing else — but
those two would otherwise each grow their own inline conditional.

**Why `None` passes through instead of becoming `""`.** They are different facts.
`NULL` means the source never said; `""` is a value that the DB would happily
store and that every reader would then have to special-case. Same reasoning as
`meta.last_synced_at` being nullable at 5.3.

**Why `.isoformat()` and not `str()`.** They happen to be identical for `date`
and `datetime` today. `isoformat()` says the format is the contract — the schema
has `CHECK (date = substr(date, 1, 10))` and `ORDER BY date` depends on ISO
sorting lexicographically. `str()` would be relying on a coincidence.

---

## `account_row`

```python
def account_row(account: Account) -> tuple:
    return (
        account.id,
        account.source.provider.value,
        ...
    )
```

**`.value` on the enum, not the enum.** `Provider` and `AccountType` use a `str`
mixin, so `sqlite3` would actually accept the member directly and store its
string — but that is an implementation detail of the mixin. Writing `.value`
means the column contains `'checking'` because we said so, not because a base
class happened to make it work. Drop the mixin one day and this keeps working.

**`account.source.provider_id` is stored unescaped.** `make_id` percent-escapes
segments before joining them; this column holds the provider's own raw id. That
is intentional — the two are different facts, and this is the one you would
compare against a provider API response.

**Why `provider` / `connection_id` are stored at all when `id` contains them.**
The rule is *never parse an id*. "Everything from this connection" has to be a
`WHERE` clause, and the alternative — `LIKE 'plaid:item_9:%'` — breaks the moment
a segment contains an escaped `%3A`. The redundancy on disk is the price of never
needing to split that string.

**Why it is a module-level function, not a method on `Account`.** A `to_row()`
method would put SQLite knowledge inside `core/models.py`, which is the one file
that must describe the domain and nothing else.

---

## `transaction_row`

Same shape. Three lines differ:

```python
txn.date.isoformat(),
```
Not `_iso` — `Transaction.date` is non-nullable, so passing it through the
None-tolerant helper would suggest it might be absent. It also enforces the
schema's `CHECK (length(date) = 10)`: a `datetime` slipping in here would produce
a 19-character string and be *rejected* rather than stored as a date-like value
that compares unequal to every bare date.

```python
int(txn.pending),
```
SQLite has no boolean. `sqlite3` would convert `True` to `1` on its own, but the
schema has `CHECK (pending IN (0, 1))` and the explicit cast is what makes the
stored value obviously the integer the CHECK is written against.

```python
encode_raw(txn.raw),
```
Never `str(txn.raw)`. Python's dict repr uses single quotes and is not JSON —
`json.loads` on it raises, so the archive would be write-only.

---

## `_upsert_sql`

```python
def _upsert_sql(table: str, columns: Sequence[str]) -> str:
    placeholders = ", ".join("?" for _ in columns)
    assignments = ", ".join(f"{c} = excluded.{c}" for c in columns if c != "id")
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {assignments}"
    )
```

**The whole reason Step 5 cared about id stability.** `ON CONFLICT(id) DO UPDATE`
is what turns a re-sync into an update instead of a duplicate — and it only works
because `make_id` is deterministic and the content-hash minter is stable. All of
that work cashes out in this one clause.

**Why not `INSERT OR REPLACE`.** It looks equivalent and is not: `REPLACE`
*deletes* the conflicting row and inserts a new one. That fires `ON DELETE`
cascades, and it discards any column the new statement does not name — so the
moment a user-set field (a category override, a note) lives on these tables, a
sync would silently wipe it. `DO UPDATE` touches only the listed columns.

**Why f-string interpolation is safe here.** `table` and `columns` are
module-level constants, never user input, and SQLite does not allow identifiers
as bound parameters — you *cannot* write `INSERT INTO ?`. The values are still
bound with `?`, which is where injection would actually matter.

**`if c != "id"` in the assignments.** The conflict is *on* `id`, so it is already
equal by definition; assigning it would be a no-op and, on a table with a
different conflict target one day, an error.

**`excluded.` is SQLite's name for the row that failed to insert** — the new
values. `SET merchant = excluded.merchant` means "take the incoming value".

```python
ACCOUNT_UPSERT = _upsert_sql("accounts", ACCOUNT_COLUMNS)
TRANSACTION_UPSERT = _upsert_sql("transactions", TRANSACTION_COLUMNS)
```
Built once at import rather than per call. Not for speed — the string build is
trivial — but so the SQL is one inspectable value. `print(repository.
TRANSACTION_UPSERT)` in a REPL is how you check what actually runs.

---

## `save_accounts` / `save_transactions`

```python
def save_accounts(conn: sqlite3.Connection, accounts: Sequence[Account]) -> int:
    conn.executemany(ACCOUNT_UPSERT, [account_row(a) for a in accounts])
    return len(accounts)
```

**`executemany`, not a loop of `execute`.** One prepared statement reused across
rows, which for 76 transactions is the difference between one parse and 76.

**No `commit()` here.** These are deliberately *not* standalone operations —
committing inside them would break `save()`'s atomicity, because a crash between
the two calls would leave accounts committed and transactions missing. They are
public because tests want to write half a sync on purpose (`test_accounts_must_
be_written_before_transactions`), but the app calls `save`.

**`Sequence`, not `Iterable`.** `len()` is needed for the return, and a generator
would be consumed by the list comprehension before `len` ever saw it.

---

## `save`

```python
def save(conn, accounts, transactions, synced_at=None) -> SyncResult:
    accounts = list(accounts)
    transactions = list(transactions)
```
Materialized once, at the top. The arguments are used twice each — the row build
and the count — and a caller passing a generator would otherwise get zero on the
second read.

```python
    stamp = (synced_at or datetime.now()).isoformat(timespec="seconds")
```
**`synced_at` is injectable for one reason: tests.** `datetime.now()` in the
middle of a function is untestable without patching a global, which is the exact
pattern `main.py` avoids by threading its `Console` through as an argument
instead of holding a module-level one. Same principle, different value.

**`timespec="seconds"`** drops microseconds. They are noise in a "last synced"
display, and the truncation makes the stored string a predictable length.

```python
    with conn:
        save_accounts(conn, accounts)
        save_transactions(conn, transactions)
        conn.execute("UPDATE meta SET last_synced_at = ? WHERE id = 1", (stamp,))
```

**`with conn:` is the atomicity.** It commits on a clean exit and rolls back on
any exception. Not `conn.commit()` at the end — that leaves a failure
half-written, and a half-written sync is worse than a failed one: the accounts
panel would show today's balances while the spending tables still described last
week, with nothing on screen saying so.

**Accounts strictly before transactions.** `transactions.account_id REFERENCES
accounts(id)` is checked per statement, so the reverse order fails on a first
sync. This is the ordering the FK makes load-bearing, and
`test_a_failed_transaction_write_rolls_back_the_accounts` pins that the rollback
covers both tables.

**The stamp is written last, inside the same transaction.** If it were outside,
a failed sync would still advance "last synced" — the dashboard would claim fresh
data while holding old rows, which is the 5.1 staleness problem inverted into a
lie. Inside, a rollback takes the timestamp with it.

```python
    return SyncResult(len(accounts), len(transactions), stamp)
```
Counts of what was *offered*, which for an upsert is also what was written —
`executemany` either applies every row or raises. It does not distinguish inserts
from updates; SQLite can report that via `changes()`, but the number a user wants
is "how much data do I have", not "how much of it was new".

---

## `last_synced_at`

```python
def last_synced_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT last_synced_at FROM meta WHERE id = 1").fetchone()
    return None if row is None else row["last_synced_at"]
```

The read half of the 5.1 staleness answer, here rather than in the read path
because it is `meta`, and `meta` is this module's table.

**`row["last_synced_at"]` by name.** `store.connect()` sets
`row_factory = sqlite3.Row` precisely so reads are by column name; `row[0]` would
break the day a column is added before it.

**Two different `None`s, deliberately collapsed.** No meta row at all, and a meta
row with a NULL stamp, both mean "never synced" to a caller. They are
distinguishable failures at *open* time — `store.init_schema` raises on a missing
row — so by the time anything calls this, the only surviving `None` is the honest
one.

---

## What this file deliberately does not do

**It never deletes.** A re-sync upserts every row the source returned and leaves
untouched anything it did not. So a transaction that *disappears* from the source
— a pending charge the bank dropped, a CSV re-exported with a correction —
survives in the database forever. That is a real gap, and it is deferred rather
than overlooked, because the fix requires a decision this step cannot make: a
delete keyed on "not in this fetch" is catastrophic against a source that returns
a 30-day window (every older row would vanish on the first sync), and the
adapters do not currently say what window they fetched. Step 5 should decide
that with the read path in hand, not now.

**It does not reconcile pending → posted.** A pending row that settles unchanged
keeps its id — the content hash excludes `pending` on purpose — so the upsert
updates it in place, and `test_a_pending_charge_that_settles_updates_the_same_
row` pins that. The unsolved half is a charge whose **amount or date moves** on
settling: it rehashes to a new id and inserts a second row beside the stale
pending one. Plaid sidesteps this by supplying its own id and swapping it; a CSV
cannot. Still open, as `CLAUDE.md` records.

**It does not validate.** The model's `__post_init__` already rejected floats and
blank merchants on the way in, and the schema's `CHECK` constraints reject them
on the way down. A third layer here would be a third place to keep in sync.
