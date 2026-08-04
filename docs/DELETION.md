# Step 5.8, part two — deletion and reconciliation

Items **3 and 4**. Both change what happens to rows already in the database, and
both landed with **zero render diff** — predicted in advance, because `removed`
is empty for fake and CSV by contract, and the recorded Plaid payload carries
`pending_transaction_id` on all 48 rows with a null value on every one.

`355 passed, 1 skipped`, `GATE PASSED`.

---

## Item 3 — hard delete

### The decision

**Hard delete, not a superseded flag.** The source is authoritative about what
it no longer has, and a ghost row would need filtering in four places —
`find_recurring`, `spending_by_category`, the recent list, and the transaction
count. Four places to forget, each failing *silently* by counting money that is
gone. A soft delete buys history nobody is asking for at the cost of a filter
every future query must remember.

This is also why the deletion policy could not be settled before 5.7: the rule
"delete anything not in this fetch" is catastrophic against a rolling window.
What makes hard delete safe here is that Plaid **states** the removal
explicitly. Sources that cannot know (fake, CSV) report `removed=[]`, and
`test_a_complete_state_source_reports_nothing_removed` holds them to it.

### The guard, and why the 5.7 id mapping pays off here

```python
result = repository.save(conn, accounts, txns, removed=[...])
result.deleted      # rows actually removed
result.unmatched    # ids that matched nothing
```

`DELETE FROM transactions WHERE id = ?` against an id that does not exist
**succeeds and removes nothing.** No error, no signal. That is precisely the
failure mode mapping ids into our own space at 5.7 was meant to prevent, and
this is where it becomes observable: `delete_transactions` checks which ids
exist *before* deleting, so a zero-match removal is counted rather than shrugged
at.

`test_a_bare_provider_id_matches_nothing_and_says_so` pins the concrete bug —
passing Plaid's raw `transaction_id` through unmapped deletes nothing at all.

**Reported, not raised.** A provider can legitimately withdraw a transaction
that arrived and vanished between two of our syncs, so aborting would break a
sync for a benign reason. `sync.py` prints a warning to stderr naming up to five
of the unmatched ids and says what a persistent count means: the two sides are
minting ids differently.

### Two implementation details worth knowing

**The existence check is chunked at 500.** SQLite caps host parameters per
statement, so a large withdrawal would otherwise raise "too many SQL variables"
— breaking exactly the unusual sync you would least want to debug.
`test_a_removal_list_longer_than_sqlites_variable_limit_works` uses
`DELETE_CHUNK * 2 + 7` ids so it keeps testing the boundary if the constant
changes.

**Deletion is inside the same transaction as the write.** A sync that failed
after deleting would leave rows gone and nothing written — data loss with no
error state to notice it by.

---

## Item 4 — pending → settled reconciliation

### Keyed on the field, not the provider

```python
PENDING_TRANSACTION_KEY = "pending_transaction_id"

def superseded_id(txn):
    prior = txn.raw.get(PENDING_TRANSACTION_KEY)
    if not prior:
        return None
    replaced = make_id(SourceRef(
        provider=txn.source.provider,
        provider_id=prior,
        connection_id=txn.source.connection_id,
    ))
    return None if replaced == txn.id else replaced
```

Deliberately **not** `if provider is PLAID`. Any source that names the row it
replaces gets this behaviour; a source that does not is untouched. That is why
fake and CSV needed no change at all — their ids survive settlement (the content
hash excludes `pending`), so the upsert already updates in place.

**The id is mapped, for the same reason as `removed`.**
`pending_transaction_id` is the *provider's* id. Deleting on it raw would match
nothing, silently — the identical failure item 3 guards against.

### The self-reference guard

`return None if replaced == txn.id` is not defensive padding. A source whose id
survives settlement would name *itself* as the row it replaces, and the delete —
which runs after the upsert — would remove the row just written.
`test_a_row_that_names_itself_is_not_deleted` covers it.

### Why a missing twin is NOT reported

`superseded` deletions are counted but their misses are **not** added to
`unmatched`, and the asymmetry is deliberate. A charge that posts between two
syncs is only ever seen settled — its pending twin was never stored, so missing
is the *common* case, not a symptom. Folding those into the warning would train
everyone to ignore the warning that matters.

That is the whole reason `delete_transactions` is called twice rather than on
one concatenated list.

### Ordering

Deletion runs **after** the upsert, so a payload carrying both the pending row
and its settled replacement resolves to one row regardless of which was written
first. `test_both_halves_arriving_together_leave_only_the_settled_row` pins that.

---

## What this does not fix

**The amount-drift case is still open, and it is the one a CSV cannot survive.**
Our content-hash ids exclude `pending`, so a row that only flips its flag keeps
its id and the upsert updates it. A charge whose **amount or date moves** on
settling rehashes to a new id and inserts a second row beside the stale pending
one.

Item 4 solves this *for Plaid*, which supplies `pending_transaction_id` and so
states the link explicitly. It does nothing for a CSV or the fixture, because
neither has any way to say "this row replaces that one" — the information is not
in the file. That is a ceiling on file-based sources, not a bug in the minter,
and it matches the duplicate-row limit already documented under **IDs**.

**Live coverage is partial.** The sandbox Item has no pending transactions and
issues no removals, so a live run exercises neither path end to end. Both were
verified against the real translation and the real writer — the Plaid payload's
`pending_transaction_id` survives translation, JSON encoding and storage, and
`superseded_id` maps it correctly — plus a live sandbox regression run
confirming syncs still work. The uncovered gap is a real Item actually
withdrawing or settling a charge.
