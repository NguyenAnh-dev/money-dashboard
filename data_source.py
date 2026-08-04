"""
data_source.py
--------------
The SELECTOR. It used to BE the data; now it only decides where data comes
from and hands back what the adapter produced.

Everything above this line -- main.py, insights.py -- still imports
`get_accounts`, `get_transactions`, and `dollars` from here and did not change
by one character when the fixture moved into adapters/fake.py. That is the
payoff of building inside-out: the seam was already in the right place, so
swapping the implementation behind it is a no-op for every caller.

  DATA_SOURCE=fake   (default)  -> adapters.fake.FakeSource
  DATA_SOURCE=csv               -> adapters.csv_source.CsvSource, reading
                                   CSV_PATH (default fixtures/bank_export.csv)
  DATA_SOURCE=plaid             -> adapters.plaid_source.PlaidSource, reading
                                   PLAID_ACCESS_TOKEN (and PLAID_ITEM_ID if
                                   set; otherwise fetched once via /item/get)

WHY THE ENV VAR IS READ PER CALL, NOT AT IMPORT
A module-level `SOURCE = _build()` would freeze the choice at first import,
which makes it untestable: a test that sets DATA_SOURCE and then imports
anything already loaded gets the old adapter. Reading inside the function
costs one dict lookup and keeps the switch honest.
"""

from __future__ import annotations

import os

from core.models import Account, Transaction
from core.money import format_money

#: What to fall back to when DATA_SOURCE is unset. Fake, deliberately: the
#: default must never be the one that makes a network call.
DEFAULT_SOURCE = "fake"


def active_source_name() -> str:
    """The configured source's name, normalized. 'fake', 'csv' or 'plaid'.

    Exists so main.py can LABEL the screen with what it is actually showing,
    without importing an adapter or re-implementing the normalization. Until
    4.7 the header was hardcoded to "fake data" -- which was fine while fake
    was the only source, and became a lie the moment a Plaid render put real
    balances on screen under it.

    That is the same failure `get_source` refuses to commit at the bottom of
    this file, pointed the other way: a dashboard quietly showing live data
    while claiming to be fake is exactly as wrong as one quietly showing fake
    data while you believe it is live.

    Does NOT validate. An unknown value still fails in `get_source`, which is
    where the raise belongs -- and sync.py fetches before it prints, so a bad
    DATA_SOURCE never reaches the header.
    """
    return os.environ.get("DATA_SOURCE", DEFAULT_SOURCE).strip().lower()


def get_source():
    """Return the adapter named by DATA_SOURCE.

    Imports INSIDE the branch, so selecting `fake` never imports the Plaid SDK
    -- which is a hard dependency the fixture has no reason to require, and a
    slow one to load for a dashboard that renders from a list of tuples.
    """
    name = active_source_name()

    if name == "fake":
        from adapters.fake import FakeSource
        return FakeSource()

    if name == "csv":
        # Inside the branch for the same reason: the fixture has no business
        # importing a csv parser, and the CSV adapter has no business being
        # loaded (or reading CSV_PATH) when nobody asked for it.
        from adapters.csv_source import CsvSource
        return CsvSource()

    if name == "plaid":
        # The branch that makes the lazy import earn its keep: plaid-python is
        # a large SDK with its own transitive dependencies, and none of it
        # should load to render a list of tuples. Note the import reaches
        # adapters.plaid_source, NOT plaid -- the adapter defers the SDK one
        # level further so its translation half stays importable without it.
        from adapters.plaid_source import PlaidSource
        return PlaidSource.from_env()

    raise ValueError(
        f"DATA_SOURCE={name!r} is not a known source. "
        f"Valid: 'fake', 'csv', 'plaid'. An unrecognised value fails loudly "
        f"here rather than silently falling back to the fixture -- a dashboard "
        f"quietly showing fake data while you believe it is live is worse than "
        f"a crash."
    )


def get_accounts() -> list[Account]:
    """Accounts from the configured source, as core.models.Account."""
    return get_source().fetch_accounts()


def get_transactions() -> list[Transaction]:
    """Transactions from the configured source, as core.models.Transaction."""
    result = get_source().fetch_transactions()
    return [*result.added, *result.modified]


def dollars(cents: int) -> str:
    """Turn integer cents into a display string. 1234 -> '$12.34'

    Kept as a thin alias so main.py does not have to change in the same step
    the formatter moves. Callers migrate to core.money.format_money in 4.2,
    which is also where the currency argument starts mattering.
    """
    return format_money(cents)
