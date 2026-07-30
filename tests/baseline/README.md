# Baseline

`dashboard.txt` is the rendered dashboard from **before** the data-model
refactor in [../../docs/DATA_MODEL.md](../../docs/DATA_MODEL.md).

## Running the gate

One command, identical in PowerShell, cmd, and Git Bash:

```
./venv/Scripts/python.exe tests/check_baseline.py
./venv/Scripts/python.exe tests/check_baseline.py --update   # deliberate only
```

Do **not** run `main.py > file` and compare by hand. `diff` is not a file
comparison in PowerShell (it aliases `Compare-Object`, which compares objects),
`/tmp/...` is not a Windows path, and `>` re-encodes the stream — so a
byte-comparison can fail on a BOM alone, with nothing wrong in your code.

## What it checks

**RENDER** — the dashboard text must byte-match this file. Catches any change to
what a human sees.

**STRUCTURE** — invariants the text is blind to. This matters more than it
sounds: nothing in the dashboard ever joins a transaction to its account.
`show_accounts` reads name/institution/balance; the transaction tables never
look up an account. So namespacing account ids to `fake:fixture:acc_1` while
transactions still carry `acc_1` renders a **byte-identical screen** on a
completely broken model. Verified: with that bug injected, RENDER passes and
STRUCTURE fails. The diff cannot do this job.

(That id is three colon-separated segments — `provider:connection_id:provider_id`
— and the format is normative, since it becomes the SQLite primary key at Step
5. See `make_id` in [core/models.py](../../core/models.py) and §2 of
[DATA_MODEL.md](../../docs/DATA_MODEL.md).)

## Why the output is reproducible

Not because stdout isn't a terminal. The real mechanisms, all three pinned
inside `check_baseline.py`:

| Variable | Why it moves | How it's pinned |
| --- | --- | --- |
| Box characters | `rich` picks ASCII `+---+` vs Unicode `┌───┐` from the **output encoding** — not terminal-ness. Windows stdout is cp1252, so a redirect gives ASCII; Linux/CI is UTF-8 and gives Unicode. Every line differs. | renders to a `StringIO`, which has no `.encoding`, so rich uses utf-8 everywhere |
| Table width | `rich` subtracts one column on Windows: 79 here, 80 on Linux | `width=80` explicitly |
| ASCII substitution | `legacy_windows` probes the Windows console and can swap in ASCII boxes independently of encoding | `legacy_windows=False` |

Colour is off via `no_color=True` rather than relying on redirect detection.
The console is **passed into `main()`**, not patched onto the module, so the pins
don't depend on `main.py`'s internal structure.

The baseline therefore contains **Unicode box characters at width 80**, and is
reproducible on any OS. If it ever shows `+---+`, it was written by the old
shell-redirect method and is machine-specific — regenerate it with `--update`.

**Honest caveat on the width pin:** it is plumbed but currently *inert*. The
widest line in this baseline is 65 columns and the net-worth panel is
`expand=False`, so 79 vs 80 produces no diff in today's output. Verified by
rendering at width 40, which does change it. A passing gate is therefore not
evidence the width pin works — it will only start mattering once some output
approaches the margin.

## Two things that will silently rewrite this file

**Trailing whitespace is significant.** `rich` centres table titles by padding
them, so four lines end in spaces (20, 24, 6, and 12 respectively). An editor
set to strip trailing whitespace on save — or a pre-commit hook that does — will
rewrite the file and the gate then fails forever on lines that look identical.
If that happens, diff the trailing spaces first.

**Line endings are pinned in git.** This repo has `core.autocrlf=true`, which
rewrites LF to CRLF on checkout; the comparison is exact, so a translated file
could never match. [`.gitattributes`](../../.gitattributes) sets
`tests/baseline/dashboard.txt -text` to disable conversion in both directions.

## The rule

**Steps 4.1–4.5 must show `GATE PASSED` with zero render diff.** They only move
code; if the screen changes, something broke. To keep that invariant clean, the
hardcoded `v0.1 - fake data` header in `main.py` **must not be touched before
4.6**, even though making it reflect the active source is tempting at 4.2. It is
cosmetic, and it would put a predicted diff inside the diff-free range.

Step 4.6 is the first deliberate break. Only run `--update` on a step that
predicted its own diff in advance.

## Known upcoming diffs

- **4.6** — the pending Starbucks and monthly Amex payment rows, plus the header
  text if you choose to make it dynamic then.

## If you add anything nondeterministic

A clock time, a "synced at" line, a random id — the render gate breaks on every
run. Filter it in `render()` before comparing.
