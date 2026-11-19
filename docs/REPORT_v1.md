# Baseline report: money-dashboard (Step 1 of `docs/PLAN.md`)

**Bottom line:** the existing checks pass and the current CSV import → dashboard path works in a throwaway database. The plan's first-release workflow does not exist yet; only the detection part is already there. Before any implementation starts, four decisions need you; they're listed at the end.

**About the archived instructions:** the session loaded the archived file's text automatically, both from your @-mention and as the start-of-session `CLAUDE.md`. Its content is byte-identical to the `CLAUDE.md` in the last commit. I didn't follow it; everything below comes from the code, tests, config and `docs/PLAN.md`.

## 1. Project and architecture
- **Stack:** Python. CI uses 3.13; the dependency file asks for 3.10 or newer. Libraries: `rich` for the terminal screen, SQLite for storage, FastAPI/uvicorn for the API, the Plaid SDK, argon2 for passwords, `cryptography` for token encryption, pytest for tests.
- **Data flow:** `sync.py` fetches from one source (fake demo data, a CSV file, or Plaid), then translates it in `adapters/*` into the models in `core/models.py`. `core/repository.py` saves it to SQLite. `main.py` reads it back, `insights.py` analyses it, and the terminal dashboard renders it. Rendering never fetches bank data.
- **API:** a separate HTTP surface (`api/`) over the same stored data, with cookie sessions.
- **Current user workflow:** set `DATA_SOURCE=csv` and `CSV_PATH`, run `python sync.py`, then `python main.py`. Database schema is version 7; version 6 is the oldest it can upgrade from.

## 2. Code vs `docs/PLAN.md`
What the plan says about existing code is accurate: the CSV format and BOM handling, one account per import, the sync/render split, the monthly detection rule grouped across accounts, the Hulu test, Netflix and Spotify in the demo data, the API recurring route (it lives at `/recurring`), and the version-6 upgrade floor.

Plan gaps I confirmed by running the code:
- **No import command.** The only CSV path is environment variables plus `sync.py`.
- **No account choice.** Every CSV import lands in one fixed "Bank Export" account. Importing a second statement into the same database merged it into that account (22 rows, 1 account).
- **No preview.** `sync.py` creates the database file on first run.
- **The import summary can't tell new rows from stored ones.** A repeat import printed "wrote 19 transactions" both times, although the stored row count correctly stayed at 19.
- **No candidate tracking, dismissal, warnings with acknowledgement, or renewal estimates.** There are no tables for any of them.
- **CSV accounts show a `$0.00` balance and net worth**, which looks like a real balance (plan Step 7).
- **Without colour, a credit looks like a charge.** Payroll renders as `$2300.00`, distinguished only by green text.
- **The plan's Netflix example (`NETFLIX.COM`) displays as "Netflix.Com".** Detection works on it: +$2.00, billing day 12.
- **Non-USD rejection is not built.** Amounts with other currency markers would probably fail as unparseable rather than with a currency message; I haven't verified this.
- **Missing documents:** there is no root README, and `docs/DATA_MODEL.md` is linked from [core/models.py:29](../core/models.py#L29) and [tests/baseline/README.md](../tests/baseline/README.md) but doesn't exist.

**Instruction conflicts:**
- **`CLAUDE.md` is deleted in the working tree.** It was moved to `docs/archive/`, which isn't committed. The plan still links `../CLAUDE.md` as "Repository guidance" ([docs/PLAN.md:15](PLAN.md#L15)), and `pytest.ini`, `.env.example` and several docs point readers to it.
- **`docs/PLAN.md` itself isn't committed**, so the plan exists only on this machine.
- **Comment style conflicts.** Your saved preference is "no code comments, explain in markdown", but the codebase is heavily commented and the archived guide asked to keep that style.

## 3. Environment variables
None are set in this shell, and there is no `.env` file.

| Name | Purpose | Required? |
|---|---|---|
| `DATA_SOURCE` | Which source to sync: `fake` (default), `csv` or `plaid` | Optional |
| `CSV_PATH` | Path to the CSV statement (relative to where you run the command) | Optional |
| `DB_PATH` | Database location (default: `money.db` in the project root) | Optional; tests and the gate override it |
| `ENCRYPTION_KEY` | Key that encrypts stored Plaid tokens | **The API won't start without it.** Tests supply their own unless it is set but empty. The CLI doesn't need it. |
| `COOKIE_SECURE` | Secure flag on the session cookie (default off) | Optional locally; must be on in any deployment |
| `PLAID_CLIENT_ID`, `PLAID_SECRET` | Plaid API credentials | Only for Plaid and the live test |
| `PLAID_ENV` | Plaid environment (default `sandbox`) | Optional |
| `PLAID_ACCESS_TOKEN`, `PLAID_ITEM_ID` | The linked Plaid account | Only when `DATA_SOURCE=plaid` |
| `CI_ENCRYPTION_KEY` | GitHub secret that CI passes in as `ENCRYPTION_KEY` | CI only |

## 4. Commands run and results
| Command | Result |
|---|---|
| `python3.13 -m venv venv` | Created `./venv` with Python 3.13.14 |
| `venv/Scripts/python.exe -m pip install -r requirements.txt` | Exit 0; all pinned versions installed |
| `venv/Scripts/python.exe -m pytest -rs -q -p no:cacheprovider` | Exit 0: **557 passed, 1 skipped, 1 warning** in 27.5s |
| `venv/Scripts/python.exe tests/check_baseline.py` | Exit 0: render ok, structure ok, **GATE PASSED** |
| `main.py` against a missing database (CSV setup) | Exit 0; "No data yet" screen, and no file was created |
| `sync.py` twice, then `main.py` (CSV sample, temp database) | All exit 0. Database: 19 rows (19 distinct IDs), 2 identical same-day rows kept, charges $2,573.34, credits $2,300.00. Dashboard shows **Hulu $19.99, +$2.00**. |
| `sync.py`, then `main.py` (default fake data, temp database) | Exit 0; 3 accounts, 76 transactions, 4 recurring charges including Netflix +$2.50 |
| Netflix example CSV, then a second statement into the same database | Exit 0; behaviour as described in section 2 |
| API smoke test on 127.0.0.1:8765 without a key | Exit 3, with a clear message about the missing key |
| API smoke test with a throwaway key | `/health` 200, `/docs` 200, data routes 401 without a session. Signup returned 422 for a `.test` email (my test input) and 201 for a `.com` one; after signup `/accounts`, `/transactions` and `/recurring` return 200 with empty lists. |
| pytest with `ENCRYPTION_KEY=""` | **Exit 1**: errors at the first API test |
| `gh run list` | Couldn't run: `gh` isn't installed |

## 5. Check status
- **Passed:** pytest, the baseline gate, the CSV and fake sync-and-render paths, and API startup.
- **Skipped on purpose:** the live Plaid test, because no credentials are set.
- **Blocked:** CI run status (no `gh` tool).
- **Don't exist:** lint, type checks, a build step, any deploy config, and a lockfile.

## 6. Confirmed problems and likely causes
- **If CI's encryption-key secret is missing, the whole suite fails** (reproduced locally above). GitHub turns a missing secret, or any pull request from a fork, into an empty value, and [tests/conftest.py:28](../tests/conftest.py#L28) only fills the key in when it's absent, not when it's empty. Whether the secret is actually configured is unverified.
- **Dependency drift.** Only top-level packages are pinned, so dependencies of dependencies install at whatever version is current. Today's install pulled starlette 1.10.0, which prints a deprecation warning about its test client. Today it's only a warning, but CI could break on a future release.
- **Untracked plan and deleted `CLAUDE.md`** (git status: `D CLAUDE.md`, `?? docs/PLAN.md`, `?? docs/archive/`). A clean checkout won't have the plan.

## 7. Missing credentials and services
Plaid credentials are missing, so only the live test is affected; it isn't needed for the plan, which uses synthetic data. There's no `.env` or `ENCRYPTION_KEY`, which only matters for starting the API by hand. No other services are needed.

## 8. Main user workflow
- **The plan's workflow (import → candidates → track → price-increase warning → acknowledge → reopen):** mostly not built. Only detection exists, and the Netflix and Hulu increases are detected.
- **The current workflow (CSV sync → dashboard):** works end to end with the sample statement in a temp database, including a safe repeat import.
- **Unverified:** behaviour with real bank exports, non-USD input, and CI.

## 9. Files changed during setup
- **In the repo, all gitignored:** `venv/` and the `__pycache__/` folders. No `money.db` and no `.pytest_cache` were created, and no tracked file changed.
- **Outside the repository:** Baseline verification used a temporary local directory for disposable test databases, a synthetic CSV statement, helper scripts, and a record of the installed dependency versions. None of these files are required to run the project.

## 10. Recommended next tasks
1. **Your decisions:**
   - Commit `docs/PLAN.md`.
   - Decide what replaces `CLAUDE.md` as repository guidance, and whether to fix the plan's link to it.
   - Settle the comment-style conflict before code is written.
2. Send this Step 1 report to Codex for review, with the empty-key failure called out as a known issue.
3. Confirm the `CI_ENCRYPTION_KEY` secret exists. Optionally, decide whether tests should also fill in the key when it's empty; that would be a small test-code change.
4. Decide whether to add a lockfile, or pin starlette and the other dependencies of dependencies. This is a dependency change, so it's your call.
5. Start Step 2 (the explicit CSV import command), covering the account-identity, preview and new-vs-stored summary gaps above.

I've stopped here and won't implement anything until you approve. I can also put this report on a shareable page for the Codex handoff if that's useful.
