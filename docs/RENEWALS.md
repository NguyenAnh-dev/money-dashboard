# Task 6: next renewal estimates

The dashboard prints an **Upcoming Renewals (estimated)** table under Price Increases. It is derived on every open from stored transactions, tracked decisions and today's date. It does not depend on `find_recurring`: the history is every eligible charge (posted, positive, not a transfer) for a tracked owner, account and merchant, so a skipped month does not drop the estimate. Pending charges never advance it. A date past 9999-12-31 is skipped instead of raising. Nothing is stored, so two opens on the same day print the same table. `main.main(console, today=None)` takes a fixed date for tests; the default is `date.today()`.

## Rules (`insights.py`, `core/repository.py`)

- Only **tracked** subscriptions are estimated. Candidates and dismissed ones are not. The history is every eligible charge (posted, positive, non-transfer) for the tracked owner, account and merchant (`repository.load_tracked_charges`), joined to `connections` on the user the same way `load_transactions` is, so a transaction whose source connection belongs to another owner is never used, so a skipped month does not drop the estimate and `find_recurring` is not involved.
- Same-day order: charges are ordered by date, then by the persisted first-observed order, then by statement row order, the same ordering the price warnings use ([PRICE_INCREASES.md](PRICE_INCREASES.md#equal-date-ordering)). The estimate amount is the last charge in that order, never decided by transaction id.
- Billing cycles (`billing_cycles`): a charge posted on day 1 to 3 of a month counts as the previous month's end-of-month cycle when at least two earlier cycles exist, all earlier cycles fell on a month's last day, and the previous cycle was exactly one month before. Aug 31, Sep 30, Oct 31 and a Dec 1 posting therefore estimates Dec 31, not Jan 1. A posting that does not meet this keeps its posted date, so a genuinely skipped month is not rewritten, and a delay without a month-end pattern is not inferred.
- Billing day (`billing_day`): the earliest day among cycle dates that were not a month's last day, otherwise the largest month-end day (Nov 30, Dec 31, Jan 31, Feb 28 gives 31).
- Expected date: the month after the latest cycle date, on the billing day clamped to that month's length. Only that one date is computed, so old history never produces a series of future renewals. A date past 9999-12-31 is skipped instead of raising.
- Shown when the expected date is from today through today plus seven days, inclusive (`RENEWAL_WINDOW_DAYS`).
- If the expected date is before today and no later charge has posted, the row stays, marked "no charge seen - import updated history". Pending charges never advance it. A posted renewal moves the estimate forward by one month.
- Latest import: the newest transaction date of any kind stored for that account (`latest_import_dates`), shown on each row.
- Display: tracked rows in Recurring Charges show `anchor_day` over their candidate evidence (the same cycle and billing-day rules), so they agree with the estimate. Untracked candidates and `GET /recurring` keep the detector's earliest day (`find_recurring` is unchanged), so month-end charges still report day 28 there.

## Snapshot

`main.main` starts an explicit read transaction (`BEGIN`) before its first query, so accounts, transactions, decisions, price warnings, tracked charges and latest import dates all come from one database state. A writer on another connection cannot commit between those reads; with the default rollback journal it waits (or fails after its timeout) until the dashboard closes the connection. The test inserts from a second connection between the transaction read and the rest and requires a coherent view.

## Tests

`tests/test_renewals.py` covers the November 5 example (Nov 12 at $17.99), day seven and day eight, month-end and leap-year clamping, the missed date at several later days, a subsequent posted renewal, per-account import dates, untracked subscriptions, and same-day repeatability, same-day ordering across separate imports (both orders), a month-end pattern with a December 1 posting, and the unchanged `/recurring` day in `tests/test_api.py`. The golden render is unchanged because the sample has no tracked subscription.

## Limits

- A tracked subscription that stopped billing (cancelled) shows "import updated history" indefinitely until it is dismissed, because the expected date never gets a charge.
- Weekend settlement that pushes a charge later does not move the estimate; the date is the billing day, not the settlement day.
- A day 1 to 3 posting after a month-end pattern is assumed to be the delayed month-end renewal; a month genuinely skipped and resumed on day 1 looks the same.
- Estimates are not available in the API or the `subscriptions.py` CLI, and `today` cannot be set from the command line.
- Prices that change between now and the renewal are not predicted.
