"""
tests/check_baseline.py
-----------------------
The refactor gate for steps 4.1-4.5. Run it from the project root:

    ./venv/Scripts/python.exe tests/check_baseline.py
    ./venv/Scripts/python.exe tests/check_baseline.py --update   # deliberate

Two independent checks, because neither one alone is sufficient:

  RENDER  -- the dashboard text must byte-match tests/baseline/dashboard.txt.
             Catches anything that changes what a human sees.

  STRUCTURE -- invariants the rendered text cannot see. The dashboard never
             joins a transaction to its account, so namespacing account ids
             while leaving transaction.account_id alone produces a
             byte-identical screen and a completely broken model. The diff
             cannot catch that. This can.

WHY THIS IS A PYTHON SCRIPT AND NOT TWO SHELL COMMANDS
The previous version of this gate ran `main.py > file` and used `diff`. That
was wrong twice over:

  1. `rich` chooses ASCII box characters (+---+) vs Unicode ones (|---|) based
     on the OUTPUT ENCODING, not on whether stdout is a terminal. Windows
     stdout defaults to cp1252, so redirecting produced ASCII. The same command
     on Linux or in CI produces Unicode box-drawing characters, and every
     single line of the diff changes for reasons that have nothing to do with
     the code. `rich` also subtracts one column from its width on Windows, so
     tables render 79 wide here and 80 wide elsewhere.

  2. `diff` and `/tmp/...` are Unix. In PowerShell, `diff` is an alias for
     Compare-Object, which compares objects rather than files, and `>`
     re-encodes the stream -- so a byte comparison could fail purely on a BOM.

Rendering in-process through a Console with encoding, width, and colour all
pinned removes every one of those variables, and `python` is the same command
in every shell.
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# THE GATE MUST BE ABLE TO PRINT ITS OWN FAILURE. Before this line, a failing
# RENDER check crashed instead of reporting: the diff echoes the rendered
# dashboard, which is full of `rich` box-drawing characters, and Windows stdout
# defaults to cp1252 -- so `print` raised UnicodeEncodeError on '│' and the
# gate died mid-report, after saying "RENDER FAIL" and before showing a single
# line of what changed. The check itself was correct; only the reporting was
# unprintable, which is the worst possible place for this bug to live.
#
# Note this is the SAME cp1252 trap the module docstring describes, arriving by
# a second route. The docstring's version is about the render (fixed by pinning
# StringIO on the Console, so `rich` always emits Unicode); this one is about
# echoing that render back out to a real console. Pinning the input encoding did
# nothing for the output path.
#
# errors="replace" is the belt and braces. Reconfiguring to utf-8 fixes every
# console that can actually handle it; `replace` means one that genuinely cannot
# render a glyph substitutes a '?' rather than taking the whole gate down. A
# mangled character in a diff is a cosmetic problem you can still read around; a
# traceback where the diff should be is not.
#
# STDERR TOO, and it is not symmetry. Adapter errors interpolate row content
# into their messages -- core.models._clean_currency reports the bad code
# ('ÉUR'), csv_source names the offending cell -- and a traceback goes to
# stderr. A sync that failed on a non-ASCII merchant would otherwise die on the
# error ABOUT the failure, replacing a precise message with a UnicodeEncodeError
# pointing at the print machinery.
#
# hasattr-guarded because `reconfigure` is a TextIOWrapper method: it is there
# for a console and a pipe, and absent when something has already replaced the
# stream with its own object (pytest's capture, some CI wrappers). Those
# replacements handle Unicode themselves, so skipping is correct, and crashing
# on the fix for a crash would be a poor trade.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# THIS IS A FAKE-DATA GATE BY DEFINITION, so say so before anything imports.
# The baseline is a snapshot of the fixture; running with DATA_SOURCE=plaid in
# the ambient shell would compare live bank data against it -- a guaranteed
# failure with a baffling message, and a network call nobody asked for.
# Set before the first import so a registry reading it at import time sees this.
os.environ["DATA_SOURCE"] = "fake"

BASELINE = Path(__file__).parent / "baseline" / "dashboard.txt"

BASELINE_SYNCED_AT = datetime(2026, 8, 1, 9, 0, 0)

#: Python floor. `X | None` needs 3.10. Note that `Path.read_text(newline=...)`
#: was only added in 3.13, which is why the file I/O below goes through
#: `Path.open()` -- read_text with that argument is a TypeError on 3.10-3.12.
MIN_PYTHON = (3, 10)
if sys.version_info < MIN_PYTHON:
    raise SystemExit(
        f"needs Python {'.'.join(map(str, MIN_PYTHON))}+, "
        f"got {sys.version.split()[0]}"
    )


def render() -> str:
    """Render the dashboard deterministically, on any OS, in any shell.

    The console is PASSED to main(), not patched onto the module. An earlier
    version reassigned `main.console`; that worked, but it depended on every
    show_* function resolving a module global at call time, which is an
    internal detail of main.py rather than a contract.

    The three pins and what each defeats:
      file=StringIO  -- a StringIO has no `.encoding`, so rich falls back to
                        utf-8 and emits Unicode box characters. Without this,
                        Windows cp1252 stdout yields ASCII `+---+` and Linux
                        yields `┌───┐`, so every line of the diff changes.
      width=80       -- rich otherwise asks the OS, and subtracts one column on
                        Windows (79 vs 80).
      legacy_windows -- the Windows console probe can substitute ASCII boxes
                        independently of encoding.
    """
    from rich.console import Console

    import main as app

    buffer = StringIO()
    with _seeded_database():
        app.main(console=Console(
            file=buffer,
            width=80,
            legacy_windows=False,
            no_color=True,
            highlight=False,
        ))
    return buffer.getvalue()


@contextmanager
def _seeded_database():
    from core import repository, store, users
    from data_source import get_accounts, get_all_transactions

    previous = os.environ.get("DB_PATH")
    with tempfile.TemporaryDirectory(prefix="baseline-db-") as tmp:
        os.environ["DB_PATH"] = str(Path(tmp) / "baseline.db")
        conn = store.open_db()
        try:
            # The user id is passed to the ADAPTER as well as to the write, so
            # the seeded rows carry the connection id sync.py would have minted
            # ("fake:fixture%3A<local user>:..."). Seeding through an unscoped
            # source would pin a set of ids no command produces, and the gate
            # would then keep passing after the sync path started writing
            # different ones.
            user_id = users.ensure_local_user(conn)
            repository.save(
                conn,
                user_id,
                get_accounts(user_id),
                get_all_transactions(user_id),
                synced_at=BASELINE_SYNCED_AT,
            )
        finally:
            conn.close()
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("DB_PATH", None)
            else:
                os.environ["DB_PATH"] = previous


# ---------------------------------------------------------------------------
# Structural invariants
# ---------------------------------------------------------------------------

def _account_key(account) -> str:
    """`id` after 4.2, `account_id` before it. The fallback dies at 4.2."""
    return getattr(account, "id", None) or account.account_id


def _txn_key(txn) -> str:
    return getattr(txn, "id", None) or txn.transaction_id


def check_structure() -> list[str]:
    """Assertions the rendered text is blind to. Returns a list of failures."""
    from core.models import DEBT_TYPES
    from core.users import LOCAL_USER_ID
    from data_source import get_accounts, get_all_transactions

    # Scoped to the same user the seed uses, so both halves of the gate are
    # asking about the ids the app actually mints. The invariants below hold
    # either way -- they are properties of the adapter, not of the owner -- but
    # a failure message naming a different id than the seed wrote would send a
    # reader looking for a second bug.
    accounts = get_accounts(LOCAL_USER_ID)
    transactions = get_all_transactions(LOCAL_USER_ID)
    failures: list[str] = []

    account_ids = {_account_key(a) for a in accounts}
    if len(account_ids) != len(accounts):
        failures.append(f"account ids not unique: {len(accounts)} accounts, "
                        f"{len(account_ids)} distinct ids")

    # THE ONE THAT MATTERS. Namespacing accounts to "fake:...:acc_1" while
    # transactions still carry "acc_1" renders identically and is still broken.
    orphans = {t.account_id for t in transactions if t.account_id not in account_ids}
    if orphans:
        failures.append(
            f"{len(orphans)} transaction account_id(s) resolve to no account: "
            f"{sorted(orphans)[:4]}. Known account ids: {sorted(account_ids)[:4]}. "
            f"If one side is namespaced and the other is not, that is the bug."
        )

    txn_ids = [_txn_key(t) for t in transactions]
    if len(set(txn_ids)) != len(txn_ids):
        failures.append(f"transaction ids not unique: {len(txn_ids)} rows, "
                        f"{len(set(txn_ids))} distinct ids")
    if any(not tid for tid in txn_ids):
        failures.append("some transactions have an empty id")

    for t in transactions:
        if isinstance(t.amount_cents, bool) or not isinstance(t.amount_cents, int):
            failures.append(f"{_txn_key(t)}: amount_cents is "
                            f"{type(t.amount_cents).__name__}, must be int")
            break
    for t in transactions:
        if not t.merchant:
            failures.append(f"{_txn_key(t)}: merchant is empty -- grouping "
                            f"happens on it")
            break

    # Debt is stored negative. Same getattr fallback as _account_key: accounts
    # have no `type` until 4.2, so this finds nothing until then, and arms
    # itself the moment Amex becomes CREDIT. `> 0`, not `>= 0` -- a paid-off
    # card sits at exactly zero and that is fine.
    for a in accounts:
        if getattr(a, "type", None) in DEBT_TYPES and a.balance_cents > 0:
            failures.append(
                f"{_account_key(a)}: debt account has positive balance "
                f"{a.balance_cents}. Debt is negative in our model, so a "
                f"positive balance means the adapter did not flip the sign, "
                f"and net worth is off by {2 * a.balance_cents}."
            )

    return failures


def main() -> int:
    update = "--update" in sys.argv
    rendered = render()

    if update or not BASELINE.exists():
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        # newline="" so no CRLF translation sneaks into the bytes on Windows.
        # Both ends go through Path.open() rather than read_text/write_text,
        # because read_text only accepts `newline` on Python 3.13+.
        with BASELINE.open("w", encoding="utf-8", newline="") as fh:
            fh.write(rendered)
        print(f"baseline written: {BASELINE.relative_to(ROOT)} "
              f"({len(rendered.splitlines())} lines)")
        if not update:
            print("NOTE: no baseline existed, so one was created. "
                  "Nothing was verified.")
            return 0

    with BASELINE.open(encoding="utf-8", newline="") as fh:
        expected = fh.read()
    render_ok = rendered == expected

    if render_ok:
        print("RENDER    ok   (byte-identical to baseline)")
    else:
        print("RENDER    FAIL")
        import difflib
        diff = difflib.unified_diff(
            expected.splitlines(), rendered.splitlines(),
            fromfile="baseline", tofile="current", lineterm="",
        )
        for line in list(diff)[:40]:
            print("   ", line)

    failures = check_structure()
    if failures:
        print("STRUCTURE FAIL")
        for f in failures:
            print("    -", f)
    else:
        print("STRUCTURE ok   (ids unique, every account_id resolves, "
              "amounts int, merchants present)")

    ok = render_ok and not failures
    print("GATE PASSED" if ok else "GATE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
