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
  DATA_SOURCE=plaid             -> adapters.plaid, later

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


def _get_source():
    """Return the adapter named by DATA_SOURCE.

    Imports INSIDE the branch, so selecting `fake` never imports the Plaid SDK
    -- which is a hard dependency the fixture has no reason to require, and a
    slow one to load for a dashboard that renders from a list of tuples.
    """
    name = os.environ.get("DATA_SOURCE", DEFAULT_SOURCE).strip().lower()

    if name == "fake":
        from adapters.fake import FakeSource
        return FakeSource()

    if name == "csv":
        # Inside the branch for the same reason: the fixture has no business
        # importing a csv parser, and the CSV adapter has no business being
        # loaded (or reading CSV_PATH) when nobody asked for it.
        from adapters.csv_source import CsvSource
        return CsvSource()

    raise ValueError(
        f"DATA_SOURCE={name!r} is not a known source. Valid: 'fake', 'csv'. "
        f"('plaid' arrives later.) An unrecognised value fails loudly "
        f"here rather than silently falling back to the fixture -- a dashboard "
        f"quietly showing fake data while you believe it is live is worse than "
        f"a crash."
    )


def get_accounts() -> list[Account]:
    """Accounts from the configured source, as core.models.Account."""
    return _get_source().fetch_accounts()


def get_transactions() -> list[Transaction]:
    """Transactions from the configured source, as core.models.Transaction."""
    return _get_source().fetch_transactions()


def dollars(cents: int) -> str:
    """Turn integer cents into a display string. 1234 -> '$12.34'

    Kept as a thin alias so main.py does not have to change in the same step
    the formatter moves. Callers migrate to core.money.format_money in 4.2,
    which is also where the currency argument starts mattering.
    """
    return format_money(cents)
