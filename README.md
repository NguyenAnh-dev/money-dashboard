# Money dashboard

A local, offline subscription tracker. It reads a bank statement CSV, finds
monthly recurring charges, warns when a tracked charge goes up, and estimates
the next renewal. Everything stays in a SQLite file on your machine. No Plaid
account or network access is needed.

## Install

Requires Python 3.10 or newer. From the repository root:

```
python -m venv venv
```

Activate it in your shell:

PowerShell:

```
venv\Scripts\Activate.ps1
```

Windows Command Prompt:

```
venv\Scripts\activate.bat
```

Bash (macOS, Linux):

```
source venv/bin/activate
```

Then install the dependencies:

```
pip install -r requirements.txt
```

All commands below run from the repository root.

## Where data lives

By default the database is `money.db` in the repository root (ignored by
git), and it holds your real data. For this walkthrough use a separate,
disposable file that does not exist yet, so the demo never touches `money.db`.
Set `DB_PATH` in the same terminal you will run every command in.

PowerShell:

```
$env:DB_PATH = "demo.db"
```

Windows Command Prompt:

```
set "DB_PATH=demo.db"
```

Bash:

```
export DB_PATH=demo.db
```

Every command below reads that file. Delete `demo.db` to start over, and
choose another name if it already exists. Statements (`*.csv`) are also
ignored by git, except `fixtures/bank_export.csv`.

## CSV conventions

- Header row with `Date`, `Description`, `Amount`. An optional `Currency`
  column must be blank or `USD`; non-USD is rejected. Only USD is supported.
- Dates are `MM/DD/YYYY`.
- **Sign:** a negative amount is money leaving the account (a charge); a
  positive amount is money coming in (a credit such as payroll or a refund).
- Amounts may use `$`, commas and a `USD` marker. Malformed rows fail the whole
  import with a row number, and nothing is saved.
- CSVs carry no balance, so balances show as unknown and are left out of net
  worth.

## Try it: from a statement to an acknowledged warning

1. Create `netflix.csv` (synthetic data):

   ```
   Date,Description,Amount
   08/12/2026,NETFLIX.COM,-15.99
   09/12/2026,NETFLIX.COM,-15.99
   10/12/2026,NETFLIX.COM,-17.99
   ```

2. **Preview** without saving: run the import and answer `n`. The database is
   not opened.

   ```
   python import_csv.py netflix.csv --account netflix-card
   ```

3. **Import**. `--yes` skips the question. Re-importing the same file adds
   nothing new.

   ```
   python import_csv.py netflix.csv --account netflix-card --yes
   ```

4. **Review** candidates. The line ends with `[id: ...]`, the stored account
   id that the next commands need. Copy it as `ACCOUNT_ID`.

   ```
   python subscriptions.py list
   ```

   A candidate needs charges in at least 3 different months on one account.

5. **Track** it. Only tracked subscriptions raise price warnings and renewal
   estimates.

   ```
   python subscriptions.py track --account ACCOUNT_ID --merchant Netflix
   ```

6. **Warnings.** After tracking, this prints
   `2026-10-12  Netflix  $15.99 -> $17.99 ...  [event: TOKEN]`, and the
   dashboard shows a Price Increases table.

   ```
   python subscriptions.py warnings
   python main.py
   ```

7. **Acknowledge** with the `TOKEN` from the event:

   ```
   python subscriptions.py acknowledge --event TOKEN
   ```

   The warning is now cleared. A later, higher charge raises a new warning.

8. **Reopen from a fresh terminal.** `DB_PATH` only lasts for the terminal
   session that set it. A new terminal that skips this setup opens the
   default `money.db` instead of your demo database and will not show the
   demo. So in the new terminal:

   - Go back to the repository root.
   - Activate the virtual environment again (see Install).
   - Set `DB_PATH` to the same disposable file you imported into, using your
     shell's command from "Where data lives" (for example
     `$env:DB_PATH = "demo.db"` in PowerShell,
     `set "DB_PATH=demo.db"` in Command Prompt, or
     `export DB_PATH=demo.db` in Bash).

   Then run:

   ```
   python subscriptions.py warnings
   python main.py
   ```

   Expected: the first command prints `no price increases`, and the dashboard
   still lists Netflix as tracked, with no Price Increases table.

## Optional: dismiss and restore

Do these only after the walkthrough above, or the demo warning is lost.

```
python subscriptions.py dismiss --account ACCOUNT_ID --merchant Netflix
python subscriptions.py restore --account ACCOUNT_ID --merchant Netflix
```

`dismiss` hides a candidate. `restore` clears the decision, tracked or
dismissed, and also discards that subscription's watch, warnings and
acknowledgements. A dismissed subscription must be restored before it can be
tracked. Tracking it again starts a fresh watch, so the latest rise can warn
again.

## When warnings and estimates appear

- **First tracking:** the newest eligible charge is compared with the
  eligible charge just before it. It warns only if it is higher. An older
  increase alone (for example 15.99, 15.99, 17.99, 17.99) does not warn.
- **Later imports:** tracking stores a date cutoff, the latest charge date
  already seen. A newly observed charge dated before the cutoff is backfill
  and never warns. A newly observed charge dated on or after the cutoff,
  including exactly on it, is compared with the charge before it and warns if
  higher.
- Upcoming renewals cover tracked subscriptions due from today through seven
  days ahead, using the usual billing day and latest amount. If the expected
  date passes with no new charge, the dashboard says to import updated history.
  The demo dates are in the past, so its renewal row shows that message.
- Credits, pending charges and transfers are not treated as subscription
  charges.

## Tests

```
python -m pytest
python tests/check_baseline.py
```

Both use synthetic data and need no credentials. Further notes are in
[docs/](docs/) (for example [CSV_IMPORT.md](docs/CSV_IMPORT.md),
[SUBSCRIPTIONS.md](docs/SUBSCRIPTIONS.md), [PRICE_INCREASES.md](docs/PRICE_INCREASES.md),
[RENEWALS.md](docs/RENEWALS.md)).
