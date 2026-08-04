# Step 5.7 — `SyncResult`

Plumbing only. `fetch_transactions()` stops returning a flat list and starts
returning a description of *what changed*. Nothing reaching the dashboard is
different, which is why the success condition was a **zero render diff**.

The gate passes byte-identical, 321 tests green.

---

## The name collision, resolved first

`SyncResult` already existed — in `core/repository.py`, returned by `save()`,
holding `accounts` / `transactions` / `synced_at`. Two types with one name
meaning opposite things (what a *source produced* vs what a *write stored*) is
the kind of thing that reads fine and confuses everyone six months later.

The repository's is now **`SaveResult`**, which is what it actually describes.
It is the smaller and less central of the two, and "save" names the operation it
reports on.

---

## 1. The type

```python
@dataclass(frozen=True)
class SyncResult:
    added: list[Transaction] = field(default_factory=list)
    modified: list[Transaction] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    cursor: str | None = None
```

**`removed` holds ids, not objects**, and the reason is not convenience. A
removed row is *gone from the source*. There is no merchant, no amount and no
date left to build an object out of — only the id we minted when the row still
existed. An adapter forced to hand back a `Transaction` here would have to
fabricate one, which is a lie no type checker would catch. `__post_init__`
rejects objects in that list explicitly, with a message saying why.

**`cursor` is `str | None`**, and `None` is meaningful: *this source has no
resume token*. That is the honest answer for a CSV and the fixture, which hand
back complete current state on every fetch. An empty string would read as a real
cursor and resume from the beginning of time.

**Every field defaults**, so `SyncResult()` is a valid empty fetch. A source with
nothing to report writes nothing.

**`frozen=True`** stops the container being reassigned. The lists inside are
still mutable — Python cannot cheaply prevent that — so this is a statement of
intent, not an enforcement.

**Why a dataclass and not a NamedTuple** (which `SaveResult` is): a NamedTuple is
iterable and index-accessible, so `added, modified, removed, cursor = result`
would work, and so would `result[0]`. For a four-field type where two fields are
lists of the same thing, positional access is a bug waiting to happen. The
dataclass makes every read a named one.

## 2. Changing `fetch_transactions()` rather than adding a method

A second method — `fetch_changes()` alongside `fetch_transactions()` — would have
avoided touching anything. It was the worse option: two near-identical methods on
every adapter, and a permanent question at each call site about which one is
correct. Adapters would drift, with one implemented properly and the other left
as a stale wrapper.

The blast radius was small where it mattered: **3 adapters, 2 production
callers.** It was large in tests — about 50 `fetch_transactions()` sites across
five files — but those are mechanical, and a test that needed real thought is a
test that was asserting something the change affected, which is the point of
having them.

`data_source.get_transactions()` still returns a flat `list[Transaction]`
(`added + modified`). It is the convenience path, and its contract did not need
to change. It discards `removed` and `cursor`, which is fine for a caller that
wanted "the transactions" — and is exactly why `sync.py` does not use it.

## 3 & 4. Fake and CSV

```python
return SyncResult(added=[...])
```

Everything in `added`; `modified` and `removed` empty; `cursor=None`.

**This is honest rather than lazy.** Both sources hand back complete current
state on every fetch. Neither has any concept of a row being *removed*: for a
CSV, a shorter file is indistinguishable from a shorter export, and for the
fixture the list is simply rebuilt each call. A source that cannot detect removal
must report none rather than guess, and
`test_a_complete_state_source_reports_nothing_removed` states that as a rule.

Note the consequence, which is the deletion question already open in `CLAUDE.md`:
these two sources can never tell the writer to delete anything, so a row that
vanishes from a CSV survives in the database forever. 5.7 does not fix that; it
makes the shape available for the sources that *can* answer.

## 5. Plaid

```python
added.extend(response["added"])
modified.extend(response["modified"])
removed.extend(response["removed"])
cursor = response["next_cursor"]
```

`/transactions/sync` was already being called and already returned all four.
Through 5.6 the adapter read `added`, followed `next_cursor` across pages, and
**threw both the cursor and the other two arrays away.**

Two real bugs were hiding in that, neither visible in any render:

- **`modified` was discarded**, so an amended charge — a tip adjustment, a
  corrected amount, a settling authorization — never reached the database. The
  stale row survived and looked fine.
- **`removed` was discarded**, so a transaction the bank withdrew stayed forever.

**`removed` is mapped into our id space here**, not passed through raw:

```python
removed=[make_id(_ref(r["transaction_id"], item_id)) for r in removed]
```

Plaid sends a bare `transaction_id`. Every id in our database is
`provider:connection_id:provider_id`, so handing the raw value upward would
produce a delete that silently matches zero rows — the worst kind of failure,
since it looks like success.

**The cursor is returned but still not applied.** The first request deliberately
carries no cursor, so a fetch still returns everything and no render can move.
`test_the_cursor_is_still_not_applied_on_the_way_in` pins that, because applying
it is the one change that would have broken the zero-diff condition. Resuming is
5.8, and it is a behaviour change with its own failure mode: a cursor that
resumes too far forward silently skips transactions.

## 6. Conformance, walked assertion by assertion

The contract was written against a flat list. Each assertion was re-pointed
deliberately rather than by search-and-replace:

**Object invariants → `added + modified`.** Ids namespaced, unique and
reconstructible; every `account_id` resolving; amounts `int`; merchants
non-empty; currency a real ISO code; dates `date` and not `datetime`; `pending` a
bool; `kind` a `TransactionKind`; no transfer reaching the recurring table. All
of these are properties of *objects*, and the objects live in those two lists. A
`_transactions(source)` helper flattens them, so the assertions read exactly as
before.

**`removed` is excluded from all of them**, and not because it happens to be
empty for every source in `ALL_SOURCES` today. It holds ids, so there is nothing
to check a merchant or a currency *on*.

**The shape test split in two.** `test_fetch_methods_return_lists` asserted both
returns were lists; that is no longer true of one of them.
`test_fetch_accounts_returns_a_list` keeps the original assertion, and
`test_fetch_transactions_returns_a_sync_result` states the new contract —
including that all three collections are lists, for the original reason: a
generator would be silently empty on a second read, and the determinism check
could not distinguish that from a match.

**Four new invariants that only exist now that the shape does:**

| Test | What it prevents |
|---|---|
| `test_removed_holds_ids_and_never_objects` | an adapter fabricating a `Transaction` to describe a deletion |
| `test_a_row_is_never_both_present_and_removed` | contradictory instructions in one payload, where whichever the writer applied last wins |
| `test_added_and_modified_do_not_overlap` | one row claiming to be both new and an update |
| `test_the_cursor_is_a_string_or_none` | an empty-string cursor reading as a real one |

## 7. `sync.py` and the repository

```python
result = source.fetch_transactions()
accounts = source.fetch_accounts()
...
repository.save(conn, accounts,
                [*result.added, *result.modified],
                cursor=result.cursor)
```

`added` and `modified` are written the same way, because the upsert already
does the right thing with both: `ON CONFLICT(id) DO UPDATE` inserts what is new
and overwrites what changed. The distinction matters to the *source*, not to the
writer.

**`removed` is deliberately not acted on yet.** Deleting rows is a policy
decision that `CLAUDE.md` records as open, and 5.7 is plumbing — acting on
removals would be a behaviour change with a render diff, which is precisely what
this step promised not to produce.

### The cursor column, and why it is scaffolding

`meta` gains `sync_cursor TEXT`, and `SCHEMA_VERSION` goes to **2**. There is no
migration path, by design, so **an existing `money.db` is now refused** with
"delete the file and re-sync" — verified, and correct: the file genuinely lacks
the column.

**One column in a one-row table is the wrong home for a cursor**, stated plainly
in the schema rather than discovered later. A cursor belongs to a *connection* —
one Plaid Item — so a database holding two Items has two cursors and one slot.
Nothing can hit that today: one adapter is held per sync, and only Plaid produces
a cursor at all. 5.8 makes syncs incremental, which is exactly when resuming from
the wrong Item's cursor would start silently skipping transactions, so that is
where the column moves to a per-connection row.

**`save` skips the write when `cursor is None`** rather than storing NULL. With
one shared slot, a `DATA_SOURCE=fake` sync into a database that also holds Plaid
rows would otherwise wipe Plaid's resume token — and the damage would stay
invisible until the next Plaid sync silently refetched everything.
`test_a_source_without_a_cursor_does_not_erase_the_stored_one` pins it.

---

## What to know before 5.8

- **Any existing `money.db` must be deleted and re-synced.** The version bump
  refuses it, loudly and with the right message.
- **`removed` now arrives and is ignored.** That is the deletion policy question,
  and Plaid answering it explicitly is the missing input that question was
  waiting for.
- **The cursor is stored but never read back into a request.** `repository.
  sync_cursor(conn)` exists to read it; nothing calls it yet.
