# Task 5a: price increase warnings

Schema **v11**. Tables, created by `CREATE TABLE IF NOT EXISTS` before migrations run (see [MIGRATIONS.md](MIGRATIONS.md) section 4). The dashboard shows a "Price Increases" table under Recurring Charges and prints nothing when there is no warning, so the baseline render is unchanged.

## Tables

- `subscription_price_observed(user, account, merchant, charge_id, seq)`: charges already evaluated for a tracked subscription. `seq` is the order they were first observed.
- `subscription_price_watch(user, account, merchant, scanned_through)`: the furthest posted date the watch has processed. It only moves forward, and it is independent of which transactions still exist.
- `subscription_price_increases(user, account, merchant, charge_id, previous_charge_id, charge_date, old_cents, new_cents)`: one warning per introducing charge, with the predecessor it was compared against.

## Evaluating charges

Eligible charges are posted, positive, non-transfer transactions on that account and merchant. They do not have to pass the candidate heuristic, so a missing month does not lose a warning.

`scan_watch` runs when a subscription is tracked and on every `repository.save` (sync and CSV import), in the same transaction:

- **Initialising** (no watch row yet): compare the latest eligible charge with its immediate predecessor and record one event only if it is a rise. Everything is marked observed and the cutoff is set to the latest date. `15.99, 15.99, 17.99, 17.99` gives none; `15.99, 15.99, 17.99` gives one.
- **Scanning an existing watch**: charges not yet observed are new. A new charge dated **before** `scanned_through` is a backfill, marked observed and never warned. A new charge dated **on or after** it is forward: it is compared with the charge before it and warns if higher. Several rises imported together each get their own event.
- **The cutoff is persisted.** It advances only from charges newly processed, never from corrections or removals. If the newest charge is removed, or its date is corrected backward, older history still looks old, and a new charge dated before the cutoff does not warn. This holds across restarts and when removal leaves no eligible charges (the watch row is kept, so it is not mistaken for a fresh start). A genuinely newer rise still warns.
- **Observation is separate from eligibility.** A charge is dropped from `subscription_price_observed` only when its transaction no longer exists for that account and merchant. Turning pending, or becoming a credit, zero or transfer, keeps its record and its first-observed position, across saves and restarts. When it is eligible again it is not new, so it is not re-evaluated and cannot jump behind other same-day charges. While ineligible it contributes to no comparison. A charge first seen while ineligible has no record, so it is evaluated when it first becomes eligible.
- A repeat import changes no ids, so nothing is new. Events are keyed by account, merchant and introducing charge.

## Equal-date ordering

A content-derived id is not evidence of posting order. Order is: date, then charges already observed in first-observed order, then charges new in this save in the order their rows were first inserted (the statement order the source supplied). A distinct charge imported later on the same day follows the earlier one. Within a single import, two same-day charges are taken in file order, so `15.99` then `17.99` warns and the reverse does not. A date-only statement cannot resolve that.

## Reconciling corrections and removals

On every save, before scanning, each stored event is re-derived from the stored transactions:

- The introducing charge is gone, ineligible (pending, credit, zero, transfer) or moved to another merchant or account: the event is deleted.
- Otherwise the predecessor is the **immediate earlier charge in the same ordering as detection**, among charges observed no later than the introducing charge. A correction that changes the order therefore changes the pair: a predecessor whose date moves past the introducing charge, a predecessor that becomes ineligible, or a charge moved between the two all lead to re-evaluation.
- If a rise is still supported, the same event is updated (predecessor, date, old, new amounts) and keeps its introducing charge id. If not, it is deleted.
- Charges observed after the event (later forward charges and backfills) are excluded, so a later decrease, or an older backfill that lands between the pair by date, does not change an earlier warning.
- **Exception: an explicit posted-date change.** `repository.save` compares each incoming transaction's date with the stored row before writing. A transaction that already existed with a different date is an explicit correction, and it is included in the comparison even if it was first observed after the warning. The first import of a previously unknown older transaction is not a correction and stays excluded. The stored predecessor always stays in the comparison, so a pair updated by a correction does not flip back on the next save.
- **The correction is persisted.** When such a charge is already observed for the watch, its id is recorded in `subscription_price_corrected(user, account, merchant, charge_id)`. That record is separate from the selected predecessor and from the charge's current eligibility, so the charge stays part of the comparison across saves and reopening the database. While it is pending it contributes nothing and the warning falls back to the next earlier eligible charge; once it posts again it returns to its corrected position, with no date change needed. The warning keeps its introducing charge and posted date, and is updated in place. The record is deleted when the transaction no longer exists for that account and merchant, and on restore. An unknown older transaction with the same date and amount has no such record and stays excluded.

Only explicit upserts and removals change stored transactions, so a row missing from a partial statement is not a deletion. Other subscriptions and cards are untouched.

## Decisions

Only `tracked` subscriptions warn. Dismissed ones are hidden and keep their rows. Restore deletes the observed, watch and warning rows with the decision, so tracking again starts fresh.

## Upgrades

- **v8 to v9** (`to_v9`): for each tracked decision, mark the latest stored eligible charges observed. No events, so no retroactive warnings.
- **v10 to v11** (`to_v11`) recovers correction records for files written before `subscription_price_corrected` existed. An earlier v10 file has no such table, so opening it creates the table empty, and without recovery an already-applied date correction would be forgotten the first time its charge went pending. The rule is a single statement: for every stored warning whose predecessor and introducing charge are both observed for the same account and merchant, and whose predecessor's observation `seq` is **later** than the introducing charge's, record the predecessor as corrected. Warnings created by scans always compare against a charge observed earlier (scans assign `seq` in date order, so a predecessor never has a later `seq` than the charge it precedes), so a later `seq` is evidence that the charge was moved into the comparison by an explicit date correction. An ordinary older backfill is observed after the warning but is not the stored predecessor, so it is never promoted. The step is `INSERT OR IGNORE`, so retrying is harmless, and it changes no decisions, transaction ids, warnings, observation order or cutoff. It also runs on files that already have the table and records nothing new.
  - *Versioning:* a bump to v11 is how a one-time data fix reaches files already stamped v10. A table created on open cannot do this, because no step runs for a file already at the current version.
  - *Not recoverable:* a correction whose warning has since been recomputed against another predecessor, or cleared, left no evidence in earlier builds. Nothing is recovered for it.
- **v9 to v10** (`to_v10`) handles three kinds of v9 file. It uses DML only; the new tables already exist.
  - *Built by this code at v9* (observed table plus an events table with `previous_charge_id`, no watch): the watch row is created from the newest observed charge date, and events are copied into `subscription_price_increases` unchanged.
  - *Earlier Task 5a v9 build* (`subscription_price_watch` with `scanned_through`, and `subscription_price_events` without `previous_charge_id`): the watch row is kept as the cutoff, charges dated at or before it are marked observed, and each event is copied only if it can be proven. The introducing charge must still exist and the charge immediately before it must match the stored old amount, new amount and date; that charge becomes `previous_charge_id`. An event that cannot be proven is **dropped rather than guessed**. It has no recoverable predecessor and would otherwise be unverifiable. The next save re-evaluates later charges normally.
  - *v8 chain*: after `to_v9`, the watch row is created from the observed charges.
- The old `subscription_price_events` table is left in place and unused. Dropping it is DDL, which a step must not do (MIGRATIONS.md section 4). Nothing reads it.
- A subscription tracked with no eligible charges at upgrade has no watch row, and is initialised when history first arrives.
- A real v7 or v6 file cannot hold decisions, so those chains just reach v10 (v6 still re-scopes transaction ids in its own step). Decisions and transaction ids are preserved on every path.

## Rendering

Merchant and account labels go through `import_csv.safe` at render time. Stored names and ids are untouched.

## Limits

- A charge that was observed, then goes pending and later posts with a different amount under the same id is not re-evaluated as a new rise, because it was already observed.
- A correction made by an earlier v10 build is recovered only while its warning still names the corrected charge as predecessor.
- A date correction is detected by comparing the stored date with the incoming one in the same save; a date changed by writing the database directly is not.
- A charge corrected back to its old price after its warning was cleared does not revive the warning, because the charge is already observed.
- A genuinely older same-day charge arriving in a later import is taken as later.
- An unprovable earlier-v9 warning is dropped on upgrade.
- Events are written only on `repository.save` and on tracking.
- The Recurring Charges table still prints merchant names unescaped.

Tests: `tests/test_price_increases.py`, `tests/test_price_reconcile.py`, `tests/test_price_cutoff_and_legacy.py` and `tests/test_price_order_and_moves.py`. The corrected table is additive and created by `CREATE TABLE IF NOT EXISTS` on any open, its recovery for older files is the v11 step above. Version and chain checks are also in `tests/test_subscriptions.py`, `tests/test_migrations.py` and `tests/test_crypto.py`.
