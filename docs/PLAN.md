# Local subscription tracker implementation plan

**Next assignment: Task 2d, correct the sync failure message at the commit boundary.** Step 1 (baseline investigation), Task 1a (offline test startup), Task 2a (explicit CSV import account selection), Task 2b (CSV import preview and validation), and Task 2c (accurate CSV import counts) are accepted. The first release is a local USD terminal dashboard where a user imports posted transactions, reviews likely monthly subscriptions, tracks or dismisses them, and sees price increases and estimated renewals when they open the dashboard.

## Baseline review

Runtime results below come from [Claude’s baseline report, sections 2, 4 and 6](REPORT_v1.md). Codex checked the relevant source and independently totaled the checked-in CSV as data. Codex did not rerun the application or tests during this review.

| Priority | Evidence and consequence |
| --- | --- |
| 1. Installation and startup | Claude installed the manifest on Python 3.13.14, ran the local CLI, and reported **557 passed, 1 skipped, 1 warning** plus a passing render and structure gate. The API started with a disposable key and refused startup without one, as designed. At baseline, CI injected a secret, the test bootstrap only handled an absent variable, and an explicitly empty encryption key broke offline API tests. Task 1a fixed that blocker. [Workflow](../.github/workflows/tests.yaml) · [Test bootstrap](../tests/conftest.py) · [API startup](../api/app.py) |
| 2. Core workflow | Importing the sample twice left 19 distinct transaction IDs. The CLI nevertheless reported 19 writes on each run. Importing a second card’s statement produced 22 transactions under one account. The selector supplies one CSV connection per user and the repository returns the input count. This establishes incorrect account attribution and ambiguous reporting. [Selector](../data_source.py) · [CSV adapter](../adapters/csv_source.py) · [Repository](../core/repository.py) |
| 3. Security and data integrity | Account selection and atomic import are the immediate local data concerns. Source review also found that multi-connection sync commits per connection while its exception message claims the entire database is unchanged. That failure path was not exercised in the baseline. The local tests used synthetic data and skipped live Plaid. No credential incident was reported. [Sync loop and CLI reporting](../sync.py) · [Save transaction boundary](../core/repository.py) |
| 4. Missing first-release behavior | The existing monthly heuristic detected the fixture’s Hulu increase. It groups by merchant across accounts. The schema and dashboard lack persistent tracking decisions, acknowledged increase warnings, and renewal estimates. [Detector](../insights.py) · [Schema](../core/store.py) · [Dashboard](../main.py) |
| 5. Usability | The CSV adapter supplies zero for an unavailable balance, and the dashboard includes it in net worth. Credits lose their direction in plain text. The example merchant displays as “Netflix.Com”. The repository also lacks a root quickstart. [CSV account](../adapters/csv_source.py) · [Renderer](../main.py) · [Merchant normalization](../adapters/merchant.py) |

The owner reports there are no existing users, external data, or deployed components. The test-client deprecation warning, hosted CI results, and behavior on real bank exports do not establish additional local startup failures. The current error for an explicit non-USD marker was unverified. Task 2b specifies the intended behavior. Keep the release focused on the signed-amount CSV layout and monthly USD subscriptions.

## Missions and permissions

- **Owner** assigns work, decides scope changes, and authorizes use of personal data or external releases.
- **Claude Code implements** one assigned task at a time, including its focused tests and documentation. An implementation assignment authorizes in-scope local edits and checks on synthetic fixtures and disposable databases. Return the diff, commands actually run, outcomes, and remaining gaps. Follow the owner’s preference for explanations in Markdown and preserve existing comments when editing code.
- **Codex reviews** the diff against the task’s acceptance criteria and reports actionable findings with file and line. Identify checks repeated independently and evidence supplied by Claude. Claude makes implementation fixes unless the owner reassigns them. The [archived instructions](archive/former-developer-instructions.md) are historical context.
- Keep secret values and environment dumps out of output. Use a test environment without Plaid credentials because a [live test](../tests/test_conformance.py) can activate when credentials are present. Obtain owner authorization before using real statements, contacting a bank, deleting a user database, pushing, or deploying.

## Ordered implementation tasks

Work from top to bottom. Each task is a separate review handoff. Use temporary databases for all verification. Keep the existing no-argument CLI and API contracts working. When a task intentionally changes the golden render, predict the change, inspect the diff, and run the independent [baseline gate](../tests/baseline/README.md).

### 1a. Remove the offline test startup blocker

- **Change.** Remove the pytest job’s secret injection. Supply a disposable test key in test bootstrap when the variable is absent or empty. Keep application startup validation intact.
- **Accept when** the full offline suite can start in either environment, and the existing missing-key startup test still observes the application’s refusal.
- **Verify** with two fresh pytest processes, one with the variable removed and one explicitly empty. Run `python tests/check_baseline.py` separately. Record results and the intentional live-test skip. [Startup regression test](../tests/test_plaid_link.py)
- **Accepted.** Commit `2463532` removed the CI secret injection and supplied the disposable test key for both absent and empty values. Codex independently ran the full suite in two fresh Python 3.13.14 processes, one with `ENCRYPTION_KEY` removed and one with it explicitly empty. Each run reported **557 passed, 1 skipped, 1 warning**, including a pass for the missing-key startup regression test. Both runs intentionally skipped live Plaid with credentials removed and reported the existing Starlette deprecation warning. The separate render and structure gate and `git diff --check` passed. Hosted CI was outside this verification.

### 2a. Give a CSV import an explicit destination account

- **Change.** Add a local command accepting a file and a stable account identifier. Reuse the adapter, the local user, and the connection-scoped save path. Choosing a file or renaming it must not change the destination account.
- **Accept when** a user can import the sample into account A and the three-row Netflix example below into B. A holds 19 rows and B holds 3. Identical row content on different cards has distinct stored IDs. Explicit CSV import fetches only the requested file.
- **Verify** through the new CLI on a fresh database. Query stored accounts and transaction owners, repeat with a renamed copy, and exercise the existing no-argument workflow with a synthetic source. [Selector](../data_source.py) · [Sync entry points](../sync.py)
- **Accepted.** Commit `98aff59` adds `python import_csv.py FILE --account ACCOUNT_ID`. Codex verified that the sample stored 19 rows in A and the Netflix example stored 3 in B, transaction ownership matched account ownership, identical rows across cards had distinct IDs, and a renamed copy preserved IDs and destinations. The CLI imported only the requested file, and no-argument sync still worked with the synthetic source. The final offline suite reported **566 passed, 1 skipped, 1 warning**, including coverage for rejected empty arguments, `.env` database selection, and literal account names. Live Plaid was intentionally skipped, and the warning was the existing Starlette deprecation. The separate render and structure gate and whitespace checks passed.

### 2b. Preview and validate before opening the database for writes

- **Change.** Parse the entire file before committing. Preview the account, date range, row count, and sample rows labeled charge or credit. Support UTF-8 with optional BOM, quoted descriptions and thousands separators, `MM/DD/YYYY` posted dates, and the headers `Date,Description,Amount`. Negative means charge and positive means credit. Bare amounts are assumed USD.
- **Accept when** preview and cancellation leave a fresh database path absent and an existing database untouched. A malformed final row or an explicit non-USD amount identifies the row and reason and commits nothing. Confirming imports the validated data.
- **Verify** the normal command with a quoted merchant, a BOM, a refund, a malformed last row, and an explicit `EUR` amount. Check the path on fresh runs and compare existing rows before and after rejection or cancellation. [CSV reader](../adapters/csv_source.py) · [CSV tests](../tests/test_csv_source.py)
- **Accepted.** Commit `736ccb2` adds a preview and confirmation step that saves the same validated rows shown in the preview. Codex verified that a CSV with a BOM, quoted merchant, thousands separator, and refund previewed and stored correctly, even when the source file was deleted at the confirmation prompt. Cancellation and rejection of malformed, non-USD, oversized, and fractional-cent rows left fresh database paths absent and preserved an existing database byte for byte. The offline suite reported **652 passed, 1 skipped, 1 warning**. Live Plaid was intentionally skipped, and the warning was the existing Starlette deprecation. The separate render and structure gate and whitespace checks passed. [Implementation notes](CSV_IMPORT.md)

### 2c. Report new and previously stored transactions accurately

- **Change.** Make the committed import summary distinguish new and previously stored rows for that account. Preserve the current content-based IDs and occurrence handling for identical rows.
- **Accept when** the first sample import reports 19 new and 0 previously stored, and the repeat reports 0 new and 19 previously stored. Both identical same-day charges survive. The sample totals remain $2,573.34 in charges and $2,300.00 in credits.
- **Verify** both CLI summaries against SQLite rows. Import an overlapping statement with one distinguishable new charge and verify only that charge is added. Document the ambiguity of identical rows across partial exports without bank transaction IDs. [Sample CSV](../fixtures/bank_export.csv) · [ID generation](../adapters/fingerprint.py)
- **Accepted.** Codex verified against SQLite that the sample reports 19 new and 0 previously stored on first import and 0 new and 19 previously stored on repeat. Both same-day duplicates survive, and the totals remain $2,573.34 in charges and $2,300.00 in credits. The partial export reports 1 new and 18 previously stored, retains all original rows, and adds only the new charge. Importing the sample into a second account reports 19 new and 0 previously stored with distinct IDs. The final offline suite reported **656 passed, 1 skipped, 1 warning**. Live Plaid was intentionally skipped, and the warning was the existing Starlette deprecation. The separate render and structure gate and whitespace checks passed. [Implementation notes](CSV_IMPORT.md)

### 2d. Correct the sync failure message at the commit boundary

- **Change.** Make CLI failure reporting describe the scope of rollback accurately. Preserve per-connection commits.
- **Accept when** a failure on a later connection tells the user earlier connections may already have been saved. A failed connection’s rows and cursor remain consistent.
- **Verify** with two synthetic sources: let the first save, then force a failure during the second save. Inspect the CLI error, first connection’s persisted rows and cursor, and second connection’s prior state. [Sync error handler](../sync.py) · [Repository save](../core/repository.py)

### 3. Detect candidates separately for each account

- **Change.** Group posted, positive model charges by owner, account, and normalized merchant. Retain the existing rule of one charge per month across at least three distinct months with 26–35 day gaps. Exclude zero amounts, credits, pending entries, and known transfers. Show account, dates, and amounts as evidence the user can review. A CSV cannot identify every transfer.
- **Accept when** three monthly Netflix charges produce one candidate on that card. Two cards cannot pool their histories to reach three observations. The sample still yields Hulu at $19.99 with a $2.00 increase.
- **Verify** with the existing Hulu and irregular-shopping cases, two cards with insufficient individual histories, and two cards each with sufficient history. Exercise the CLI and the existing API recurring-route tests. [Analysis](../insights.py) · [CSV cases](../tests/test_csv_source.py) · [API route](../api/routes/insights.py)

### 4. Persist track, dismiss, and restore decisions

- **Change.** Expose those three actions on a local candidate, identified by owner, account, and merchant. Add only the persistent state needed for the decisions. Use the existing schema upgrade mechanism.
- **Accept when** decisions survive restarting, reimporting, and a missing month. A dismissed candidate stays hidden until restored. Changing one card’s decision leaves the other card alone.
- **Verify** each action through the CLI, close and reopen the database, and repeat the import. Test a fresh database, an upgrade from schema v7, and the supported v6 upgrade chain with synthetic data. Check transaction IDs and foreign-key integrity after migration. [Store](../core/store.py) · [Migration guidance](MIGRATIONS.md)

### 5a. Emit one warning for each observed increase

- **Change.** Use consecutive eligible posted charges on the tracked account. At initial tracking, compare the latest charge with its immediate predecessor. For later imports, identify each new increase by the subscription and the transaction that introduced the price. Display merchant, account, date, old and new amounts, and difference.
- **Accept when** tracking the example produces one $2.00 warning. An unchanged next charge keeps that warning visible. A decrease, pending charge, repeat import, or older backfill produces no new current warning. A subsequent, distinct increase generates its own warning.
- **Verify** with the example, a flat month, a pending charge, a decrease followed by a return to the old price, and an older overlapping import. Check event counts and posted dates as well as displayed amounts. [Existing price comparison](../insights.py)

### 5b. Acknowledge the specific increase

- **Change.** Add a local acknowledge action and persist acknowledgement for that event. Dismissed subscriptions should have no visible warnings.
- **Accept when** acknowledging the same event twice is harmless. Reopening and reimporting leave it cleared. A later rise on the same subscription remains independently actionable.
- **Verify** by acknowledging the example warning, exiting, reopening, reimporting, and introducing another increase. Check both stored state and the warning list. Apply the migration checks from Task 4 to any new schema change.

### 6. Estimate the next monthly renewal

- **Change.** Use the observed billing day and latest posted amount for tracked subscriptions. Show estimates due today through seven calendar days ahead, inclusive, alongside that account’s latest imported transaction date. Clamp the billing day to the end of shorter months.
- **Accept when** the example, viewed on November 5, estimates November 12 at $17.99. After the expected date passes without a new charge, show that updated history is needed. Old history must not keep generating future renewals.
- **Verify** using a fixed local date for today, day seven, day eight, month-end, a stale statement, and a subsequent posted renewal. Two opens on the same day must agree.

### 7a. Make terminal amounts and warnings readable

- **Change.** Show increases and upcoming renewals first, followed by tracked subscriptions and candidates. Label credits and charges in plain text. Show CSV balances as unknown and identify the accounts included in any net-worth subtotal. Normalize the sample descriptor `NETFLIX.COM` to “Netflix” while preserving the raw descriptor and transaction ID.
- **Accept when** a no-color display distinguishes payroll from spending, a CSV-only account shows unavailable net worth, and a genuine known zero balance can still display as zero. Empty states tell a new user how to import data or how many monthly observations are needed.
- **Verify** no-color renders with an absent database, insufficient history, no active alerts, a known zero balance, a CSV-only account, and mixed known/unknown balances. Reimport the Netflix rows after normalization and compare IDs. Inspect the intentional golden diff. [Renderer](../main.py) · [Merchant normalization](../adapters/merchant.py)

### 7b. Publish a local quickstart in the repository and verify the release

- **Change.** Write a root README with exact installation and local commands for preview, import, review, track/dismiss/restore, and acknowledgement. Describe CSV sign and currency conventions, local storage, and when warnings appear. Correct stale guidance links encountered in this workflow.
- **Accept when** a new user can follow the README from a fresh temporary database to an acknowledged Netflix warning that stays cleared on reopening. The demo requires only a synthetic statement and local dependencies.
- **Verify** by following the README literally, then running `python -m pytest` and `python tests/check_baseline.py` separately. Report pass/fail/skip counts, baseline result, and any intentional output changes. Check paths referenced by the quickstart. No hosted release is part of this task.

## Synthetic statement for acceptance checks

Use one fresh account for this example. The current detector already finds the increase. The new end-to-end check is whether a user can track it, see the dated warning, acknowledge it, and reopen without seeing it again.

```csv
Date,Description,Amount
08/12/2026,NETFLIX.COM,-15.99
09/12/2026,NETFLIX.COM,-15.99
10/12/2026,NETFLIX.COM,-17.99
```

## Next handoff

Task 2d awaits the owner’s assignment. Once assigned, Claude implements Task 2d only and returns the diff and verification results. Codex returns **accepted**, **changes requested**, or **verification pending** with the reason before the next handoff.
