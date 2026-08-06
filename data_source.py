"""
data_source.py
--------------
The SELECTOR. It used to BE the data; now it only decides where data comes
from and hands back what the adapter produced.

Everything above this line -- main.py, insights.py -- still imports
`get_accounts`, `get_all_transactions`, and `dollars` from here and did not change
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

from core.models import Account, Transaction, scoped_connection_id
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


def _for_user(connection_id: str, user_id: str | None) -> str:
    """The connection id an adapter should mint under, for this owner.

    Unscoped when no user is named, which keeps `get_accounts()` and a bare
    `FakeSource()` producing the ids they always did -- that is the inspection
    path, and the conformance suite's.

    Scoped when a user IS named, because `fake.CONNECTION_ID` and
    `csv_source.CONNECTION_ID` are one constant shared by everybody. Two users
    syncing the fixture would otherwise mint the SAME account ids and the second
    sync's upsert would overwrite the first user's rows -- which is why
    `repository.ensure_connections` refused the write outright. Scoping is what
    removes the collision rather than reporting it. See docs/USERS.md §6.

    Plaid is deliberately not routed through here: an Item id is already unique
    per user per institution, so scoping it would rekey every linked account to
    buy a property it already has.
    """
    if user_id is None:
        return connection_id
    return scoped_connection_id(connection_id, user_id)


def get_source(user_id: str | None = None):
    """Return the adapter named by DATA_SOURCE, minting under `user_id`.

    Imports INSIDE the branch, so selecting `fake` never imports the Plaid SDK
    -- which is a hard dependency the fixture has no reason to require, and a
    slow one to load for a dashboard that renders from a list of tuples.
    """
    name = active_source_name()

    if name == "fake":
        from adapters.fake import CONNECTION_ID, FakeSource
        return FakeSource(connection_id=_for_user(CONNECTION_ID, user_id))

    if name == "csv":
        # Inside the branch for the same reason: the fixture has no business
        # importing a csv parser, and the CSV adapter has no business being
        # loaded (or reading CSV_PATH) when nobody asked for it.
        from adapters.csv_source import CONNECTION_ID, CsvSource
        return CsvSource(connection_id=_for_user(CONNECTION_ID, user_id))

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


def get_accounts(user_id: str | None = None) -> list[Account]:
    """Accounts from the configured source, as core.models.Account."""
    return get_source(user_id).fetch_accounts()


def get_all_transactions(user_id: str | None = None) -> list[Transaction]:
    """EVERY transaction the source has, as core.models.Transaction.

    Named for the guarantee, since 5.8 made it possible to get less. A source
    handed a cursor returns only what changed, and `SyncResult.added` then means
    "new to us" rather than "everything" -- so a caller wanting a complete
    statement has to say so. `cursor=None` is passed explicitly for the same
    reason: a full replay here is a decision, not the shape of the default.

    This is the convenience path. sync.py deliberately does not use it -- it
    holds one source, resumes from the stored cursor, and needs `removed` and
    the new cursor, all three of which this function drops on the floor.
    """
    result = get_source(user_id).fetch_transactions(cursor=None)
    return [*result.added, *result.modified]


def dollars(cents: int) -> str:
    """Turn integer cents into a display string. 1234 -> '$12.34'

    Kept as a thin alias so main.py does not have to change in the same step
    the formatter moves. Callers migrate to core.money.format_money in 4.2,
    which is also where the currency argument starts mattering.
    """
    return format_money(cents)
