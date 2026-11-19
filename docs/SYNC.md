# `sync.py`, line by line

Step 5.6. The command a person runs when they want fresh data. It fetches
through `data_source`, writes through `repository`, and prints what happened.

`main.py` is the other half. Its changes are documented further down this file.

---

## Why this file exists at all

Step 5.1 chose to split fetching from rendering. This is the fetch half, and
everything about it follows from that one decision: it is the only entry point
that touches the network, the only one that loads a provider SDK, and the only
one that needs credentials. `main.py` is now incapable of all three, which is
the actual prize — a dashboard that cannot be made slow, or broken, by a bank.

## Imports

```python
from core import repository, store
from data_source import active_source_name, get_source
```

The inverse of `main.py`'s import list, and deliberately so. This file talks to
adapters and to the database. `main.py` talks only to the database.
`test_main_imports_neither_data_source_nor_any_adapter` pins that the split
stayed split.

## `sync()`

```python
def sync() -> repository.SyncResult:
    source = get_source()
    transactions = source.fetch_transactions()
    accounts = source.fetch_accounts()

    conn = store.open_db()
    try:
        return repository.save(conn, accounts, transactions)
    finally:
        conn.close()
```

**Separated from `main()` so the work is callable without the printing.** A
future scheduled sync, or a test, wants the result and not the stdout.

### One source instance, held (5.7)

The first version called `data_source.get_accounts()` and `get_transactions()`,
which each call `get_source()` internally — so every sync **built two adapters**.
For the fixture and a CSV that is a wasted file read. For Plaid it is two SDK
clients and, with `PLAID_ITEM_ID` unset, a second `/item/get`, because
`PlaidSource` caches `_client` and `_item_id` **on the instance** and both caches
died with the first adapter.

`data_source.get_source()` was made public for this. `get_accounts()` and
`get_transactions()` are unchanged and still build their own — they are the
convenience path for anything that wants one list.

### Why transactions are fetched BEFORE accounts

This looks backwards, since `repository.save` writes accounts first. Fetch order
and write order are independent, and the fetch order closes a real gap rather
than narrowing it.

Two fetches are two moments, T1 and T2. The provider can change between them.

- **Accounts first.** Accounts are a snapshot at T1; transactions arrive from T2.
  A transaction for an account opened between T1 and T2 references an account
  that is *not in the list we hold* — the foreign key rejects it, and the whole
  sync fails. Correct behaviour, baffling message.
- **Transactions first.** Transactions come from T1; accounts are a snapshot at
  T2, which is *later*. Every account a T1 transaction could reference already
  existed at T1, so it is in a T2 snapshot. The orphan case cannot arise.

The residual risk is an account **removed** between T1 and T2, which is far rarer
than one being added and is the deletion question already open in `CLAUDE.md`.

Holding one instance narrows the window; the ordering removes the failure. Both
are worth having — the window still exists for anything else the two calls might
disagree about (a balance that moved), it just no longer breaks the write.
`test_sync_fetches_transactions_before_accounts` pins the order, because it reads
as an accident and would be "tidied" back.

**`store.open_db()` with no argument** — it resolves `DB_PATH` itself, per call.
Passing a path here would mean two places knew how to find the database.

**`try/finally` rather than `with`.** `sqlite3.Connection.__exit__` commits or
rolls back the *transaction*; it does not close the connection. `repository.save`
already owns the transaction boundary, so what is needed here is the close, and
`finally` is what says so.

**Accounts before transactions is not visible here, and that is correct** — the
ordering the foreign key makes load-bearing lives inside `repository.save`, in
one place, where every caller inherits it. A sync script that had to remember it
would be a sync script that could forget it. See above for why the *fetch* order
is the other way round.

## `main()`

```python
    source = active_source_name()
    path = store.db_path()

    print(f"source    {source}")
    print(f"database  {path}")
```

**Both printed BEFORE the fetch**, so a sync that hangs on a network call has
already told you what it is talking to and where it intends to write. A progress
line that only appears on success is not a progress line.

**`active_source_name()` is correct here and wrong in `main.py`.** Same function,
opposite verdicts, and the distinction is the whole of 5.6: the env var describes
what is *about to be fetched*, which is exactly what this command is doing. It
describes what was *last stored* not at all, which is why the dashboard header
had to stop using it.

**`store.db_path()` is printed because `DB_PATH` is invisible otherwise.** The
most likely confusion in the whole step is syncing into one database and
rendering from another; both commands name the file they used, so the two
outputs can be compared.

```python
    try:
        runs = sync()
    except SyncFailed as exc:
        ...  # names the failed connection, then the connections already saved
        raise
    except Exception as exc:
        ...  # failed before any connection was saved
        raise
```

**The rollback guarantee is per connection, not per run.** `repository.save`
wraps one connection's accounts, transactions, cursor and timestamp in a single
transaction, so a failure inside it leaves that connection's stored data as it
was. But `sync()` calls `save` once per connection, so connection A has already
committed when connection B fails. The old message, "nothing was written -- the
database is unchanged", was false in that case. It was also overstated when
nothing had been saved: opening the database can create or migrate the schema,
and `ensure_local_user` can commit a row, both before the first connection runs.

**`SyncFailed` carries what the handler needs**: the failing `ConnectionRef`,
the `ConnectionSync` results whose saves completed, and the original exception
(chained with `from`, so the traceback still shows the real cause). The messages
are worded in terms of connection sync data, not "the database":

- The failed connection "did not finish saving, so this run changed none of its
  stored accounts, transactions or cursor". It does not say "rolled back",
  because a fetch failure happens before any save transaction starts.
- If earlier saves completed, they are listed as already saved and staying
  saved.
- If none completed, the message says so and adds that the database file,
  schema and local user may already exist.
- A failure outside the per-connection loop (building sources) takes the plain
  `Exception` branch with the same "before any connection save completed"
  wording.

Per-connection commits are kept on purpose: one broken bank login should not
discard another's good sync, and a re-run resumes from each connection's own
stored cursor.

**`raise` at the end, not `return 1`.** The traceback is the diagnostic, and this
project prefers loud failures. The two printed lines add context *ahead of* the
traceback rather than replacing it.

**To stderr, not stdout**, so `python sync.py > log.txt` still shows the failure
on the terminal.

**A bare `except Exception`** — justified because it re-raises. It is not
swallowing anything; it is annotating.

```python
    print(f"wrote     {result.accounts} accounts, "
          f"{result.transactions} transactions")
    print(f"synced    {result.synced_at}")
    return 0
```

**`result.synced_at` is the string that was written to `meta`**, not a freshly
formatted `datetime.now()`. Formatting the time twice from two sources is how a
log and a database end up disagreeing by a second, and the point of printing it
is that it matches what `main.py` will show.

**Counts come from `SyncResult`** rather than from a `SELECT COUNT(*)`, so the
line reports what this run wrote rather than what the database happens to hold.
An upsert means those numbers differ — the second sync of the same data writes
76 and the table still holds 76.

## The `__main__` block

```python
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    raise SystemExit(main())
```

**`.env` is loaded here and nowhere else**, matching `main.py`'s rule exactly:
reading process-wide config is an entry point's job. An adapter that loads `.env`
on import is an adapter you cannot construct twice with different config.

This is now the file that genuinely needs it — Plaid credentials are a sync
concern, not a render one.

**`raise SystemExit(main())`** propagates the exit code, so a failed sync in a
script or a cron job is detectable without parsing the output.

---

# `main.py`, the changed lines

## The import list

```python
import os

from core import repository, store
from core.money import format_money as dollars
```

**`data_source` is gone entirely**, and that absence is the shape of Step 5.1
rather than an import tidy-up. Rendering reads the database; fetching is
`sync.py`'s job. The payoff is negative capability: this file *cannot* touch the
network, *cannot* load a provider SDK, and *cannot* be made slow by a bank being
slow.

**`dollars` now comes from `core.money` directly.** It used to be imported from
`data_source`, which held a thin alias — so keeping that import would have meant
importing the whole selector for a formatting function, defeating the point.
`format_money` is the same function the alias pointed at, so the render is
unchanged.

**`os` is new**, for the one existence check below.

## `show_no_data`

The empty-database screen, and the failure mode 5.1 **accepted** when it split
sync from render. With no `sync.py` run, every table would draw with zero rows
under a $0.00 net worth — which reads as a *broken* dashboard rather than an
*unsynced* one, and those need very different responses from the person looking
at it. It is also the first thing a new user hits.

It prints **the path it looked at**. A `DB_PATH` pointing somewhere unexpected
produces a screen identical to never having synced, and the path is the only
thing that distinguishes the two.

## `source_label`

```python
def source_label(providers):
    return ", ".join(providers)
```

Fed by `repository.stored_providers()`, which is `SELECT DISTINCT provider FROM
accounts`.

**Why `active_source_name()` had to stop supplying this.** It reads `DATA_SOURCE`
— which describes what the *next sync would fetch*, while the screen shows what
the *last sync stored*. Those diverge the moment the two commands separate, so
`DATA_SOURCE=plaid python main.py` against a fixture-synced database would have
labelled fake data "plaid". That is the same class of lie the hardcoded "fake
data" header told at 4.7, arriving through a door that only opened at 5.6.

**Why it joins rather than picks.** One database can legitimately hold two
sources — syncing `csv` and then `fake` leaves both in the file, with no id
collision. `"csv, fake data"` is ugly and true, which beats naming whichever
synced most recently and mislabelling the rest.

## `format_synced_at`

```python
    if stamp is None:
        return "never"
    return stamp.replace("T", " ")[:16]
```

**Deliberately absolute, never relative.** "3 days ago" is what a person actually
wants to read and it cannot be baselined: the stored timestamp is fixed while
`now` moves, so the gate would pass on the day it was recorded and fail every day
after. A gate that expires is worse than a slightly less friendly footer.
`test_the_footer_is_absolute_never_relative` asserts the words "ago", "days",
"today" and "yesterday" appear nowhere in the render.

**`None` becomes `"never"`** rather than blank, so a database that exists with an
unstamped meta row says so.

## `main`

```python
    path = store.db_path()

    if not os.path.exists(path):
        show_no_data(console, path)
        return
```

**The existence check comes before `store.open_db()`, which would CREATE the
file.** Creating a database as a side effect of a read-only command is how an
empty `money.db` ends up in whatever directory someone happened to run this
from — and it then renders "no data" forever while a perfectly good database
sits elsewhere. `test_rendering_never_creates_the_database` pins it.

```python
    conn = store.open_db(path)
    try:
        accounts = repository.load_accounts(conn)
        transactions = repository.load_transactions(conn)
        synced_at = repository.last_synced_at(conn)
        providers = repository.stored_providers(conn)
    finally:
        conn.close()
```

**The connection closes before anything renders.** Nothing below that line
touches the database — the model objects are already built — so holding it open
across the render would be holding a file lock for the duration of some `rich`
table arithmetic.

```python
    if not accounts:
        show_no_data(console, path)
        return
```

**A file that exists but holds nothing is the same situation as no file**: a sync
that has not happened, or one that failed and rolled back. Same screen, because
the same action fixes it.

```python
    console.print(f"[dim]last synced {format_synced_at(synced_at)}[/dim]")
```

**The line `meta.last_synced_at` exists for.** With sync and render split,
week-old data renders exactly as confidently as fresh data, and this is the only
thing on screen that says which one is in front of you.

---

# `tests/check_baseline.py`, the changed lines

## Why the gate had to change at all

`render()` calls `main.main()`, which now reads a database. Left alone, the gate
would have compared the baseline against **whatever the user last synced** — a
file whose contents depend on when somebody last ran `sync.py`, and which may
hold live bank data. Determinism is the entire point of this script, and
pointing it at mutable state would have ended it.

## `BASELINE_SYNCED_AT`

```python
BASELINE_SYNCED_AT = datetime(2026, 8, 1, 9, 0, 0)
```

A **requirement** of the gate, not a convenience. `main.py`'s footer prints
`meta.last_synced_at`, so an unpinned value would put the wall clock into the
rendered text and the diff would fail on every run after the one that recorded
it. `repository.save` takes `synced_at` for exactly this, which keeps the render
a pure function of the fixture.

The date is one day after the fixture's last transaction (2026-07-31), so the
baseline reads as a coherent moment rather than an arbitrary one.

## `_seeded_database()`

A throwaway database holding exactly the fixture, for the render only.

**`DB_PATH` is now the fourth pin**, alongside `file=StringIO`, `width=80` and
`legacy_windows=False`. It is as much an input to the render as the console is.

**The temp directory is deleted on exit, including when the render raises**, so a
failing gate does not leave databases behind.

**The previous `DB_PATH` is restored rather than deleted.** A caller that had it
set for its own reasons gets it back, and `check_structure()` runs after
`render()` returns.

## What did *not* change

`check_structure()` still reads through `data_source`, deliberately. It asks
whether the **adapter's** model is intact — a question about the source, not
about what happens to be stored. It also gives a far better error message than
the foreign-key violation the same bug would cause during the seed.
