# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Use the venv interpreter explicitly — it works identically in PowerShell, cmd, and Git Bash:

```
./venv/Scripts/python.exe main.py                           # render the dashboard (fake)
$env:DATA_SOURCE="csv"; ./venv/Scripts/python.exe main.py   # render from fixtures/bank_export.csv

./venv/Scripts/python.exe tests/check_baseline.py           # THE gate — run before every commit
./venv/Scripts/python.exe tests/check_baseline.py --update  # rewrite the baseline (deliberate only)
./venv/Scripts/python.exe -m pytest                         # unit tests
./venv/Scripts/python.exe -m pytest tests/test_money.py::test_bare_int_is_whole_units -q
```

**Two test commands, on purpose.** They cover different things and neither replaces the
other:

- **pytest** (`pytest.ini`, `testpaths = tests`, `python_files = test_*.py`) runs the unit
  tests: `test_money.py`, `test_csv_source.py` (the two CSV failures a render is blind to —
  an inverted `SIGN` and an unstable id), and `test_conformance.py` (the adapter contract).
  Adopted at step 4.3 ahead of need, because the conformance test is natively a
  `@pytest.mark.parametrize` over adapters and it was not worth learning pytest and
  designing that contract in the same sitting.
  `pythonpath = .` in `pytest.ini` is what puts the project root on `sys.path`; without it
  `import core.money` fails, because pytest only adds `tests/` itself.
- **`check_baseline.py`** is deliberately **not** collected (its name does not match
  `python_files`, and it is a script with a `main()` returning an exit code, not a module
  of `test_` functions). Converting it is churn for no gain today — it works, it is the
  release gate, and its whole design is about controlling the process it runs in. Run it
  separately, and treat `GATE PASSED` as the thing that must be true before a commit.

**`check_structure()` duplicates several conformance assertions on purpose.** The gate has
to keep working as a standalone script in a process it fully controls; importing shared
assertions from the test suite would couple the release check to the thing it is supposed
to be independent of. The assertions are four lines each — the independence is worth more
than the de-duplication. The split: `check_baseline.py` asks *does the configured source
still render byte-identically, and is its model intact* (fake only, by definition);
`test_conformance.py` asks *does every adapter honour the contract*, regardless of
`DATA_SOURCE` and regardless of whether it renders at all.

No linter and no build step.

Dependencies (`requirements.txt`): `rich` at runtime, `pytest` for tests. Nothing under
`core/`, `adapters/` or `main.py` may import pytest. Python 3.10+ required (`X | None`
unions).

## The baseline gate

`check_baseline.py` runs two independent checks, and both must pass (`GATE PASSED`):

- **RENDER** — the dashboard must byte-match `tests/baseline/dashboard.txt`.
- **STRUCTURE** — invariants the rendered text is blind to: ids unique, every
  `Transaction.account_id` resolves to an `Account.id`, amounts are `int`, merchants
  non-empty, debt accounts non-positive.

STRUCTURE exists because the dashboard **never joins a transaction to its account**.
Namespacing account ids while leaving `transaction.account_id` bare renders a
byte-identical screen on a completely broken model. Never assume a passing render means
the model is intact.

Determinism is achieved by rendering in-process through a `Console` with `file=StringIO`,
`width=80`, `legacy_windows=False`, `no_color=True` — pinning encoding, width, and colour.
Do **not** replace this with `main.py > file` + `diff`: `rich` picks ASCII vs Unicode box
characters from the *output encoding* (Windows cp1252 redirect → ASCII, Linux → Unicode),
subtracts a column on Windows, and PowerShell's `diff` is `Compare-Object`. See
`tests/baseline/README.md` for the full rationale, including the trailing-whitespace and
`.gitattributes` (`-text`) traps that can silently rewrite the baseline.

Only run `--update` on a change that predicted its own render diff in advance.

## Architecture

Data flows strictly one direction, and the layering is the point:

```
adapters/*  →  core/models  →  insights.py  →  main.py
(translate)    (vocabulary)    (analysis)      (rich rendering)
                    ↑
              data_source.py (selector, reads DATA_SOURCE env var per call)
```

- **`core/models.py`** — the app's own vocabulary. **No third-party imports, ever**; the
  moment `plaid` or `requests` appears there the model describes a vendor instead of the
  domain. Invariants enforced in `__post_init__`: money is integer minor units (never
  float — `_require_int_cents` rejects floats and bools), **positive = money left the
  account**, ids are namespaced by provider, `merchant` is never empty.
- **`core/money.py`** — the single place amounts get formatted (and, later, parsed). Handles
  per-currency minor units (JPY 0, KWD 3). Deliberately no thousands separators — adding
  them moves the baseline.
- **`data_source.py`** — selector only. Reads `DATA_SOURCE` **per call**, not at import, so
  tests can change it; imports the adapter *inside* the branch so choosing `fake` never
  loads a provider SDK. An unknown value raises rather than falling back to fake.
- **`adapters/`** — each source translates its records into `core.models`. `fake.py` is the
  reference implementation of the contract: a class with `fetch_accounts()` /
  `fetch_transactions()`, plus module-level `PROVIDER` and `CONNECTION_ID` constants that
  feed every id it mints. Nothing above this layer learns where data came from.
  `csv_source.py` adds the crosswalk pattern every future bank copies — `COLUMNS`, `SIGN`,
  `DATE_FORMAT`, `CURRENCY` as named constants at the top of the file. **`SIGN = -1` is the
  most dangerous line in the codebase**: the export writes debits negative (OFX
  convention), our model writes them positive, and an inverted flip has *no visible
  symptom* — every number still formats, every total still ties, and spending and income
  have swapped places. It has its own test for that reason. A CSV supplies no category
  (everything is `Uncategorized`), no balance (`CsvAccount.balance_cents` defaults to 0 —
  summing the rows would give the period's net change while looking exactly like a
  balance), and no account identity, which is why that is constructor-supplied.
  `plaid_source.py` is **split in two on purpose**: `to_account` / `to_transaction` are
  module-level functions over plain dicts (no network, no SDK, no credentials), and
  `PlaidSource` is the I/O around them. Every invariant the contract checks is a property
  of the translation, so that split is what lets `fixtures/plaid_sandbox.json` enforce the
  whole contract on every run. Its traps, all found in the recorded payload rather than the
  docs: amounts arrive as **floats** (`_to_cents` goes via `str()`, never `int(x*100)`);
  `merchant_name` is **null on half the rows**, so the raw name falls through
  `adapters/merchant.py`; and a 401k balance carries **four decimal places**, which is why
  `_balance_to_cents` rounds while `_to_cents` refuses to — a balance is a snapshot that
  already moved, a transaction amount is what somebody was charged.
- **`adapters/merchant.py`** — the shared descriptor → merchant normalizer, moved out of
  `csv_source.py` at 4.7 when Plaid needed the same fallback. Adapters must not import each
  other, and its error message no longer names a CSV column.
- **`insights.py`** — pure analysis over model types. `find_recurring` is charges-only by
  design (skips `amount_cents < 0`, so payroll is excluded) and requires ≥3 distinct
  (year, month) pairs, exactly one charge per month, and every gap within 26–35 days.
  It also **skips `pending`** — a decision, not an oversight. A pending charge's amount is
  provisional (tip adjustments, fuel holds, FX), so a pending row compared against its own
  settled history manufactures a price change, which is the one thing this detector exists
  to report. A subscription is worth detecting one month late; a fabricated "your price
  went up" alert is worth nothing. Pending rows still count everywhere else — spending
  totals, the recent list, the transaction count — because the money is committed even
  when the number is not final.
- **`main.py`** — rendering only. **No module-level `console`**: the target is threaded
  through as an argument so the gate can pin it by passing a `Console`, not by patching a
  global.

### Adding a provider

`tests/test_conformance.py` is what "adding a provider" now means. It holds `ALL_SOURCES`
and parametrizes the entire contract over it, so a new adapter inherits the full suite by
adding **one entry** to that list — ids
namespaced, unique and reconstructible from their own `SourceRef`; every
`Transaction.account_id` resolving; amounts `int`; merchants non-empty; debt non-positive;
currency a real ISO code; dates `date` and not `datetime`; and two fetches producing
identical ids.

The list holds *instances*, not classes, because constructor shapes differ — `FakeSource()`
takes nothing, `CsvSource()` a path and an account, Plaid's will take an access token — and
that list is the one place that knows how to build each one.

Two of those assertions are worth knowing about before writing an adapter:

- **`test_ids_are_exactly_what_make_id_produces`** rebuilds each id from the record's own
  `SourceRef` rather than picking the string apart. That is stricter than a prefix check:
  it catches an adapter minting a plausible-looking id under the wrong connection.
- **`test_the_source_list_covers_every_provider_that_has_an_adapter`** asserts that *no*
  `Provider` is missing from `ALL_SOURCES`. It named `PLAID` as the one exemption until 4.7;
  that exemption is spent, so a new enum member now fails here until it is registered.

**Plaid broke the "one entry" promise, and the reason is worth knowing.** `FakeSource` and
`CsvSource` are pure — same input, same output, no setup. `PlaidSource` needs credentials,
a network and a token from a multi-step handshake, so it cannot simply be constructed in a
list. It participates as **two** entries instead:

- `RecordedPlaidSource` drives the real `to_account` / `to_transaction` from
  `fixtures/plaid_sandbox.json`. No credentials, no network, runs everywhere. **This is the
  one that enforces the contract**, because every assertion is about the translation rather
  than the connection.
- The live `PlaidSource` runs in a separate test that **skips when credentials are absent**,
  catching only what a recording cannot: the I/O half drifting from the translation half.
  Note pytest deliberately does **not** load `.env`, so this skips even when `.env` is
  populated — otherwise every test run would hit the network.

A skipped test reads as green, which is why Plaid does not participate by skipping alone.

**Re-recording the fixture** (`scripts/get_plaid_token.py --record-only`) churns the whole
file: sandbox regenerates data per Item with dates relative to the recording date. Record
against the token already in `.env` — minting again returns a token for a *different* Item,
and the fixture would then describe accounts that token cannot reach.

The module pins **both** `DATA_SOURCE` and `CSV_PATH` before importing adapters. It builds
sources directly rather than through `data_source`, so the env var has no say — but a test
whose meaning depends on the shell it ran in is worse than no test. `CSV_PATH` is the one
that actually bites: `CsvSource()` falls back to a *relative* path, so without the pin a
bare `CsvSource()` only works when pytest is invoked from the project root.

### IDs

Every id is `provider:connection_id:provider_id`, built by `core.models.make_id`, with
segments percent-escaped (`%` first, then `:`) so concatenation stays injective; empty
segments raise. **Never parse an id** — read provenance from the `SourceRef` instead.
Keep it case-sensitive when it becomes a SQLite primary key (`COLLATE NOCASE` would merge
distinct records).

Sources that supply no id of their own (CSV, the fixture) mint one from row *content* via
`adapters/fingerprint.py` — never from row position, which would rekey everything below an
inserted row. Hash the **raw descriptor**, not the normalized merchant: improving the
merchant cleaner must not move historical ids. Changing `DIGEST_CHARS` rekeys every row
ever minted.

**The duplicate-row limit.** Two rows with identical content — same date, descriptor,
amount, account, which is two coffees on one morning, not an error — hash identically,
because the source recorded nothing that tells them apart. `ContentIdMinter` folds an
occurrence number into the second and later copies, so the ids are unique and stable *as
long as the export is*. What that buys and what it does not:

- **Safe:** re-exporting with the duplicates in a different order. They are identical, so
  permuting them yields the same set of ids. Inserting unrelated rows anywhere is also
  safe — only the new row gets a new id.
- **Not safe:** the *count* of duplicates changing. Drop one of three identical charges (a
  refund, a corrected export) and the survivors renumber, so at Step 5 the upsert rewrites
  rows instead of removing one.
- **Arbitrary either way:** which real-world charge is occurrence #0 versus #1. If a user
  attaches a note or category override to one of them, a re-import can land it on the
  other. Nothing can fix this from the file alone — the information was never exported.

Treat this as a known ceiling on CSV id stability, not a bug to solve in the minter. The
real fix is upstream: a source that supplies its own ids (Plaid does) never reaches this
code path.

## Project conventions

- This is a staged learning project ("Step 4.1", "Step 5", …). Steps 4.1–4.5 were
  **diff-free refactors** — they moved code and had to leave the render byte-identical.
  Step 5 introduces SQLite, which is why id stability matters now.
- **The header reports the active source** (`v0.1 - fake data` / `csv data` / `plaid data`),
  via `data_source.active_source_name()`. It was hardcoded through 4.5 so those refactors
  stayed diff-free; the pin expired at 4.7, when a Plaid render put real balances on screen
  under the word "fake". That is the failure `_get_source`'s error message argues against,
  pointed the other way — claiming one thing while showing another. Note the fake render is
  **byte-identical** either way, which is why fixing this moved no baseline.
- Comments here explain *why*, often at length, and frequently name the specific bug a
  line prevents. Match that register when editing; don't strip it as verbose.
- The fixture in `adapters/fake.py` has traps baked in on purpose (Chipotle at a constant
  $14.42 on varying days; Amazon descriptors with per-order codes that defeat naive string
  grouping; Uber trips vs Uber Eats). Don't "clean" them up.
- **The recurring table has FIVE rows, and the fifth one is wrong on purpose.** The monthly
  Amex card payment is a *transfer* between two accounts the user owns, and the model has
  no way to say so yet, so it satisfies every rule the detector has: monthly, one charge
  per month, gaps of 31 and 32 days. Three visible consequences, all deliberate — it is
  detected as a fifth subscription; its $1634.49 double-counts spending already counted
  when the Amex purchases posted, and now outranks Groceries as the top category; and its
  varying amounts produce a `+$242.39` price-rise alert, which is meaningless for a card
  payment. **4.8 introduces transfer handling and the count returns to four.** Until then,
  a "bug report" about any of the three is the fixture working. Don't fix it by deleting
  the row — that discards the demonstration 4.8 is built against.
- `docs/DATA_MODEL.md` is referenced from several files but does not exist yet.

## Deferred, on purpose

- **Pending → posted reconciliation is Step 5 work**, not now. Today a pending row is just
  a row with a flag: nothing reconciles it when it settles, because with no database there
  is nothing to reconcile *against* — each run rebuilds every transaction from scratch. At
  Step 5 the same charge arrives twice, pending then posted, and the id decides what
  happens. Our content-hash ids deliberately exclude `pending` (see `adapters/fake.py`'s
  `add()`), so a row that only flips its flag keeps its id and the upsert updates it. What
  is *not* solved: a settling charge whose **amount or date moves** — a tip added, an
  authorization posting a day later — rehashes to a new id and inserts a second row
  alongside the stale pending one. Plaid sidesteps this by supplying its own id and
  swapping it on settle; a CSV cannot. Decide the strategy when the database exists, not
  by pre-emptively weakening the hash.
- **`Account.institution` is hardcoded to `"Plaid"`** for every Plaid account, so a Plaid
  render reports "3 institutions" as 1 and the net-worth panel's "across all banks" is
  aspirational. The real name needs a second API call (`/institutions/get_by_id` off
  `item.institution_id`), which buys a footer line in exchange for another network round
  trip and another failure mode. Deliberately not taken: it is *cosmetically* wrong, not
  *dishonestly* wrong — nothing claims the name is real, unlike the header did.
- **The `pending` skip in `find_recurring` is currently unenforced.** The fixture's only
  pending row is a Starbucks, which `find_recurring` already rejects for having 15 charges
  across 3 months — so deleting `if txn.pending: continue` changes no output and no test
  fails. Making it bite needs a pending charge on an otherwise-clean monthly subscription,
  asserted in a unit test rather than through the render. Worth doing before anyone trusts
  the rule.
