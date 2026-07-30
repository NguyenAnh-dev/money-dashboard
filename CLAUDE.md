# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The project uses a local venv at [venv/](venv/) (Python 3.13, Windows):

```powershell
.\venv\Scripts\python.exe main.py          # run the dashboard
.\venv\Scripts\pip.exe install -r requirements.txt
```

There is no test suite, linter, or build step yet. Sole runtime dependency is `rich`.

## Architecture

Three modules, deliberately layered so the data source can be swapped without touching anything else:

- [data_source.py](data_source.py) — DATA layer. Owns the `Account` and `Transaction` dataclasses and currently returns hardcoded fake data from `get_accounts()` / `get_transactions()`. Also owns `dollars()`, the only cents→display formatter.
- [insights.py](insights.py) — LOGIC layer. Pure functions over lists of `Transaction`/`Account`: `total_balance`, `spending_by_category`, `find_recurring`. Knows nothing about display; imports nothing from `main.py`.
- [main.py](main.py) — PRESENTATION layer. `rich` tables only. Each `show_*` function renders one section; `main()` composes them.

The direction of imports is strictly `main.py → insights.py`, `main.py/insights.py → data_source.py`. Keep it that way — the point of the layering is that Step 4 (below) drops in a real Plaid client behind `data_source.py` and nothing upstream changes.

### Roadmap the code is written against

The docstrings and [.env.example](.env.example) reference numbered steps. Step 4 = add `get_plaid_data()` returning the *same shape* as the fake functions, selected via the `DATA_SOURCE` env var (`fake` | `plaid`). Step 5 = SQLite persistence (`*.db` is already gitignored). Don't restructure the modules in ways that break those seams.

## Domain invariants

These are load-bearing; violating them silently corrupts results.

- **Money is integer cents, never a float.** `$12.34` is `1234`. Every field is `*_cents`. Formatting happens only at the edge via `dollars()`.
- **Sign convention: positive = money spent, negative = money received.** `spending_by_category` filters `amount_cents > 0`; `find_recurring` skips `amount_cents < 0` because recurring *income* (payroll) is a separate cash-flow feature by design, not a subscription.
- **Group by `merchant`, never by `name`.** `name` is the raw bank descriptor and carries per-order noise (`AMZN Mktp US*2X4B1QR03`); `merchant` is the normalized name. Grouping on `name` shatters one merchant into many. When real Plaid data arrives this is the first thing that breaks.
- **Never assume transactions are sorted.** Plaid returns newest-first. `find_recurring` re-sorts before computing date gaps.

## `find_recurring` specifics

The detector requires: ≥3 distinct `(year, month)` pairs, exactly one charge per month (`len(txns) == len(months)`), and every consecutive gap within 26–35 days. Year is part of the month key so a 12-month pull can't merge May 2026 with May 2027.

Price-change tracking compares `latest_cents` against *the last amount that differs* — not `amounts[-2]`, which would hide a rise that then held steady. `RecurringCharge.delta_cents` / `.direction` are properties so callers (CLI now, API later) report changes identically instead of each recomputing.

The billing anchor is `min(day)` because weekend settlement only pushes a charge forward, never back.

## Fake data is a test fixture

[data_source.py](data_source.py) `get_transactions()` has adversarial cases baked in on purpose, documented in its docstring: Chipotle hits the same $14.42 in all three months on different days (defeats amount-only matching), Amazon's descriptor changes every time, Uber trips vs. Uber Eats share a prefix but differ in category, T-Mobile varies in amount, some merchants skip a month. Expected true positives are Spotify, Netflix (price rises in July), Planet Fitness, T-Mobile. When changing detection logic, check the output against that list before and after.
