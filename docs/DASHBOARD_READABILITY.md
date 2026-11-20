# Task 7a: readable terminal dashboard

## Layout (`main.py`)

1. Price Increases, then Upcoming Renewals. When both are empty one line says so ("No price increases or upcoming renewals").
2. Recurring Charges: tracked subscriptions first, then candidates; dismissed ones are hidden. With nothing to show, the screen says a subscription needs charges in at least 3 different months on one account and points at `python import_csv.py FILE --account ACCOUNT_ID`.
3. Accounts and net worth, Spending by Category, Recent Transactions.

The "no data" screen now names both the CSV import command and `python sync.py`.

## Credits and charges

Recent Transactions has a **Type** column: `Charge` for a positive model amount (money left), `Credit` for a negative one (money arrived, e.g. payroll or a refund). Amounts stay unsigned, so the distinction survives without colour.

## Unknown balances (schema v12)

`accounts.balance_known` (and `Account.balance_known`, default true) records whether the source supplied a balance. The CSV adapter always writes false; the Plaid adapter writes false when `balances.current` is absent (the stored `balance_cents` is still the placeholder `0`). `main.balance_known` only reads the flag, so a genuine `$0.00` is shown and included.

- The Balance column shows `unknown` for flagged accounts.
- All balances known: the net-worth panel is unchanged ("Net Worth (across all banks)").
- Some unknown: "Net Worth (known balances only)" with the subtotal and the included and excluded accounts.
- None known: `unavailable`, with the excluded accounts listed.
- Account labels in the panel and the Accounts table go through `safe()`, so a newline or escape in an account name is printed as `
` or `` instead of forging a line.

The API is unchanged: it still returns `balance_cents` (0 for an unknown balance) and no flag.

**Historical data.** The v12 migration sets the flag to false for every stored CSV account, which is exact. Plaid rows written before v12 cannot be told apart: a missing `current` and a real zero both stored `0` and nothing else differs. They stay "known" until the next `sync.py` run rewrites the account with the flag; that is the recovery path.

## Merchant normalization and subscription state

`NETFLIX` was added to `adapters/merchant.py`, so `NETFLIX.COM` becomes `Netflix`. Transaction IDs are minted from the raw descriptor and do not move; the raw descriptor stays in the transaction name.

Decisions and price history are keyed by merchant, so renaming the merchant would orphan them (open warnings vanish, a dismissed candidate resurfaces, acknowledged warnings return). The v12 migration therefore rekeys them:

1. A transaction is a Netflix descriptor when it starts with `NETFLIX`, unless it is a Plaid row whose stored payload (`raw`) has a nonblank `merchant_name`. That is the same rule as `plaid_source.merchant_of`: an explicit provider name is kept, so a later replay cannot undo the rename. Plaid rows that used the descriptor fallback are normalized like CSV rows. Descriptor rows still carrying the old two-word fallback (`Netflix.Com`) get merchant `Netflix`.
2. The old key of each Netflix descriptor is recomputed from its preserved raw descriptor, so state is found even when every transaction was already reimported (and already says `Netflix`) by an earlier Task 7a build. For each (account, old key) with no transactions left under the old key, the six merchant-keyed tables are rekeyed to `Netflix`. If other rows still carry the old name, that key is left alone, because it is shared with something the migration cannot reattribute.
3. If the old and new keys already coexist (for example a database reimported by an earlier build of Task 7a): the newest `decided_at` decision wins and a tie goes to `dismissed`, because resurfacing a hidden candidate is the harm being prevented. The observed ledger is the union, renumbered from 1. The two ledgers have independent `seq` counters, so they are interleaved by the transaction's stored row position (`transactions.rowid`, the order rows were first inserted and the same tie-break the price code uses for unledgered charges), while each ledger keeps its own internal order. This preserves the recorded order of same-day charges and of backfills across both keys; a charge in both ledgers keeps the old entry's place; an entry whose transaction is gone inherits the previous entry's position.

   **How existing warnings keep their history (schema v12 `history` column).** Row position says when a transaction was first inserted, not when it posted, and v11 stores no posting time, so the order of charges from the two keys is ambiguous. Two warnings can even demand opposite orders (`Netflix` $10 to $12 and `Netflix.Com` $14 to $16 on the same dates), which no single merged ledger can satisfy. So the merged ledger is only a best guess, and each warning that existed in either key at upgrade time stores its own pre-merge history: `subscription_price_increases.history` holds a JSON object: `seen`, the charge IDs that key's ledger had observed up to and including the warning's charge, and `foreign`, the charges that were recorded as corrected only under the other key. Reconciliation evaluates a warning with a stored history over exactly those charges (plus its recorded predecessor and any moved or corrected charge, except the `foreign` ones, so a correction made under the other key can never become this warning's predecessor), ordered by date and the merged ledger, instead of everything with a lower merged `seq`. Charges from the other key, and anything observed later, are therefore treated like a backfill. Corrections still work: if a charge in the history changes, or the warning's charge is removed or no longer exceeds its predecessor, the warning is updated or cleared. Acknowledgements are keyed by charge ID and are unaffected. Warnings created after the upgrade have no stored history and use the ordinary ledger rule, so a later increase stays independently actionable. The `history` column is null when only one key had a ledger.

Because rows are rekeyed when the file is opened, a later full or partial reimport sees one consistent key.

## Schema v13 and databases stamped v12 by an earlier build

The `history` column was added to v12 after some builds had already stamped files v12, and those files open fine but fail on save with `no such column: history`. Schema v13 adds the column (`to_v13`, guarded so a file that already has it is untouched) through the same atomic `BEGIN IMMEDIATE` step as every other migration; reopening is a no-op. A v11 file passes through both steps.

**What can be recovered if an earlier build already merged the ledgers.** The two pre-merge ledgers and the per-key corrected sets no longer exist, so no history can be rebuilt for those warnings. The `history` column is null for them, and each warning keeps its recorded predecessor and amounts. For a file an earlier build already merged pair by pair, a stored history may be missing the `foreign` entries of a later alias, and that cannot be repaired because the other key's corrected set is gone; the warning keeps its recorded predecessor and amounts, and one whose predecessor still agrees with the merged ledger order survives replays; one that conflicts with it (for example two warnings that need opposite same-day order) is dropped by the next reconcile and can come back only through the scan watch for the latest increase. Acknowledgements are keyed by charge ID and are kept. Files that go through the v11 to v12 step with this build get exact histories.

## Alias groups (more than two keys)

An account can carry several legacy keys at once (`Netflix`, `Netflix.Com`, `Netflix.Com Ca`, ...). The migration first moves every legacy transaction, then decides per account which legacy keys can be merged (no unrelated rows left under the key) and handles that whole group in one step. Before any key is rekeyed it reads, for every key in the group, the original ledger, the original corrected set and the warning rows, and writes each warning's `history` from that snapshot. The invariant is: **a warning's `seen` is its own key's ledger up to its charge, and its `foreign` is the union of the other group members' corrected charges minus its own key's corrected set, so the result does not depend on which alias is processed first.** (An earlier version snapshotted pair by pair and skipped a warning that already had a history, so a correction contributed by a later alias leaked into the warning and the warning vanished on replay in one alias order only.)

A correction recorded under several keys is foreign to a warning only if the warning's own key did not also hold it; if it did, it was eligible under the original key and stays eligible. The merged ledger interleaves all member ledgers by row position with a fixed tie-break (position, then key name), decisions use the newest `decided_at` among all members with ties going to dismissed, scan watches keep the latest date, and alias rows are moved in sorted key order so identical-primary-key conflicts resolve the same way every time. Histories are only written for warnings that have none and only when at least two group members had a ledger; a warning already carrying a history (from an earlier build) keeps it.

## Atomicity

Each migration step starts with `BEGIN IMMEDIATE` and rechecks the stored version while holding the write lock. Two connections that both read version 11 therefore serialize: the second waits (up to the connection timeout), sees the version already stamped 12, and applies nothing (`upgrade` returns only the steps this caller applied). `PRAGMA foreign_keys` is switched off before the transaction because SQLite ignores it inside one.

`migrations._apply` opens an explicit `BEGIN` before each step. Python's implicit transaction starts only at the first INSERT, UPDATE or DELETE, so the v12 `ALTER TABLE` used to commit by itself; a later failure left `balance_known` added while the version stayed 11. Now the column, data changes and version stamp commit or roll back together, and a retry after a failure succeeds.

## Recovery limits for earlier Task 7a builds

An earlier build that reimported everything while state stayed under `Netflix.Com` also ran the price reconcile, which deleted the open increase, the observed ledger and the corrections under the old key. What survives is the decision, the scan watch and any acknowledgement. After the v12 rekey, the next track or import scan re-derives an increase only for charges dated on or after the watch date (so the latest increase comes back, and stays cleared if it had been acknowledged because acknowledgements are keyed by charge ID). An older increase the earlier build deleted is not recoverable.

## Limits

- Net-worth totals in the API still count placeholder zeros.
- Plaid zeros written before v12 are not recoverable without a resync.
- The rekey is specific to Netflix; a future prefix-table entry needs its own migration step.

## Tests

`tests/test_readable_render.py` (render, balances, label sanitization, ordering), `tests/test_merchant_rekey.py` (legacy and coexisting keys, full and partial reimports, acknowledgement and renewal continuity); the golden render intentionally changed (sections reordered, Alerts line, Type column). Section-slicing helpers in the price, renewal and subscription tests were updated for the new order.
