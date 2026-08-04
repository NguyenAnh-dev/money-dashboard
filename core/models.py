"""
core/models.py
--------------
The app's own vocabulary for money. THIS FILE HAS NO THIRD-PARTY IMPORTS, and
that is a rule, not an accident: the moment `plaid` or `requests` appears here,
the model has started describing a vendor instead of the domain.

Every data source -- Plaid, a bank CSV, Teller, the fake fixture -- gets an
adapter that translates INTO these types. Nothing above the adapter layer
(insights.py, main.py) ever learns which provider the data came from.

THE INVARIANTS. An adapter that violates one of these is broken, and the
conformance test in tests/ exists to catch it:

  1. Money is INTEGER MINOR UNITS (cents), never a float.
       $12.34 -> 1234
     Floats lose precision (0.1 + 0.2 != 0.3), and in fintech that is how you
     silently lose money. Note that the leak is usually not in storage but at
     the PARSE -- see core/money.to_cents.

  2. POSITIVE means money LEFT your account. Negative means it came in.
     Plaid happens to agree with us; most other providers follow the OFX
     convention and need a sign flip inside their adapter.

  3. IDs are namespaced by provider, so two sources cannot collide.

  4. `merchant` is never empty. Grouping happens on it.

See docs/DATA_MODEL.md for the reasoning behind all of this.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class Provider(str, Enum):
    """Where data came from. `str` mixin so it serializes as plain text."""
    FAKE = "fake"        # the built-in fixture
    PLAID = "plaid"      # the reference implementation; the standard we map to
    CSV = "csv"          # a bank's exported statement
    # TELLER = "teller"; SIMPLEFIN = "simplefin"; GOCARDLESS = "gocardless"


class AccountType(str, Enum):
    """Ours, deliberately coarse. Adapters map their taxonomy into this.

    Plaid, for instance, has a type/subtype pair with dozens of combinations.
    Collapsing it here means the mapping table lives in ONE place (the adapter)
    instead of being re-derived by every display loop.
    """
    CHECKING = "checking"
    SAVINGS = "savings"
    CREDIT = "credit"
    INVESTMENT = "investment"
    LOAN = "loan"
    OTHER = "other"


class TransactionKind(str, Enum):
    """WHAT a transaction is, as opposed to what it was spent on.

    Distinct from `category` on purpose, and the distinction is the whole point
    of this enum. `category` is a DISPLAY STRING each source spells its own way:
    the fixture writes "Transfer", Plaid writes "Transfer In"/"Transfer Out",
    a CSV writes "Uncategorized". Keying behaviour off that string works on one
    source and quietly fails on the other two -- so analysis reads THIS, which
    every adapter sets deliberately, and never the text beside it.

    TRANSFER is the member that currently earns its keep. Money moving between
    two accounts the user owns is not spending: the purchases were already
    counted when they were charged, so counting the card payment too reports
    money that was never spent (see insights.spending_by_category), and a
    payment that varies month to month satisfies every rule find_recurring has
    while being nothing like a subscription.

    The others are declared now rather than added one at a time because the set
    is the taxonomy; an adapter author needs to see the whole thing to pick
    correctly. INCOME/FEE/REFUND are carried by adapters that can classify them
    and are not yet read by anything -- that is fine, they are non-null and
    correct the day an insight wants them.
    """
    PURCHASE = "purchase"   # ordinary spending
    TRANSFER = "transfer"   # money between your own accounts
    INCOME = "income"       # money in: payroll, interest
    FEE = "fee"             # the bank charging you
    REFUND = "refund"       # money back from a merchant
    OTHER = "other"


#: Account kinds where a positive provider balance means "you owe this much".
#: Plaid reports a credit card's balance as positive debt; our model says debt
#: is negative, so these need a flip that a checking account does not. Get it
#: wrong and net worth is off by TWICE the card balance.
DEBT_TYPES = frozenset({AccountType.CREDIT, AccountType.LOAN})


@dataclass(frozen=True)
class SourceRef:
    """Where one record came from -- the TYPED view of its provenance.

    This overlaps `Account.id` / `Transaction.id` on purpose. The id is an
    opaque primary key; this is how you READ provenance without splitting that
    string. Never parse the id: a provider_id containing a colon would break
    the split, and `fetched_at` is not in there at all.
    """
    provider: Provider
    provider_id: str                     # the provider's own id for this object
    connection_id: str                   # one linked login: a Plaid Item, one
                                         # CSV import batch, one enrollment
    fetched_at: datetime | None = None   # changes on EVERY sync, which is
                                         # exactly why it is not in the id --
                                         # the id must stay stable so a re-sync
                                         # updates one row instead of inserting
                                         # a second. Moves to the connection
                                         # record once there is a database.


def make_id(source: SourceRef) -> str:
    """The collision-proof primary key for an account or transaction.

    Deterministic on purpose: re-syncing the same transaction produces the same
    id, which is what turns a database write into an upsert instead of a
    duplicate-detection problem.

    Exactly three colon-separated segments: provider:connection_id:provider_id,
    e.g. "fake:fixture:acc_1" or "plaid:item_9:aBc123".

    KEEP IT CASE-SENSITIVE when this becomes a database primary key. Provider
    ids are routinely mixed-case, so a `COLLATE NOCASE` column would silently
    merge "aBc123" and "abc123" into one row -- two distinct transactions, one
    surviving. Plain TEXT PRIMARY KEY in SQLite is already case-sensitive; the
    risk is only in adding NOCASE on purpose.

    The segments are ESCAPED, because plain concatenation is not injective:

        connection_id="a:b", provider_id="c"   -> plaid:a:b:c
        connection_id="a",   provider_id="b:c" -> plaid:a:b:c

    Two distinct records, one key, and at Step 5 one of them silently loses to
    the other on upsert. Same failure class as the NOCASE trap above, so it gets
    the same treatment: made impossible rather than documented.
    """
    return ":".join((
        source.provider.value,          # enum value: no colons by construction
        _escape_segment(source.connection_id, "connection_id"),
        _escape_segment(source.provider_id, "provider_id"),
    ))


def _escape_segment(segment: str, field_name: str) -> str:
    """Make a segment safe to join with ':' without losing injectivity.

    '%' is escaped FIRST and that order is not optional -- otherwise the literal
    input "a%3Ab" and the escaped form of "a:b" would both render as "a%3Ab" and
    we would have reintroduced the collision we are removing.

    Empty segments are rejected: an id like "fake::acc_1" means an adapter
    forgot to set connection_id, which is worth failing on loudly rather than
    writing a row keyed on a blank.
    """
    if not segment:
        raise ValueError(
            f"{field_name} must not be empty -- an id with a blank segment is "
            f"an adapter bug, not a valid key"
        )
    return segment.replace("%", "%25").replace(":", "%3A")


@dataclass
class Account:
    id: str                     # ours, from make_id()
    source: SourceRef
    name: str                   # "Chase Checking"
    institution: str            # "Chase"
    type: AccountType
    balance_cents: int          # SIGNED: debt is negative (see DEBT_TYPES)
    currency: str = "USD"       # ISO-4217

    # Optional, but cheap to carry now and annoying to retrofit later.
    mask: str | None = None              # last 4 digits, for disambiguating
                                         # two cards at the same bank
    available_cents: int | None = None   # differs from balance while charges
                                         # are still pending
    as_of: date | None = None            # a balance is a snapshot, not a fact

    def __post_init__(self) -> None:
        self.currency = _clean_currency(self.currency)
        _require_int_cents(self.balance_cents, "balance_cents")
        if self.available_cents is not None:
            _require_int_cents(self.available_cents, "available_cents")


@dataclass
class Transaction:
    id: str                     # ours, from make_id()
    source: SourceRef
    account_id: str             # -> Account.id (OURS, never the provider's)
    date: date                  # posted/booked date. Fall back to authorized
                                # only when posted is null, which happens while
                                # a charge is pending -- mixing the two adds
                                # spurious day-jitter and breaks the recurring
                                # detector's gap window.
    name: str                   # RAW descriptor, exactly as received:
                                #   "AMZN Mktp US*2X4B1QR03"
    merchant: str               # NORMALIZED, and never empty: "Amazon".
                                # Grouping happens on this, so a source that
                                # does not supply one gets it derived in core.
    amount_cents: int           # POSITIVE = spent, NEGATIVE = received
    category: str               # "Food & Drink"
    currency: str = "USD"
    kind: TransactionKind = TransactionKind.PURCHASE
                                # WHAT it is, not what it bought. DEFAULTED, and
                                # the default is the design: a source that
                                # cannot classify (a CSV has no category data at
                                # all) neither breaks nor has to invent a guess,
                                # and every consumer still reads a non-null
                                # value on every row. PURCHASE is the safe
                                # default because it is the only one that
                                # changes no behaviour -- defaulting to OTHER
                                # would drop unclassified rows out of whatever
                                # a later insight decides OTHER means.
    pending: bool = False       # not yet settled; amount and date may still
                                # change, and Plaid swaps the id when it posts
    raw: dict = field(default_factory=dict, repr=False)
                                # the provider's original payload. Excluded
                                # from repr so debug output stays readable and
                                # a full API response never lands in a log line
                                # by accident.

    def __post_init__(self) -> None:
        self.currency = _clean_currency(self.currency)
        _require_int_cents(self.amount_cents, "amount_cents")


@dataclass(frozen=True)
class ConnectionRef:
    provider: Provider
    connection_id: str

    def __post_init__(self) -> None:
        if not self.connection_id:
            raise ValueError(
                "connection_id must not be empty -- it is half the key a "
                "cursor is stored under, and a blank one would collide with "
                "every other blank"
            )


@dataclass(frozen=True)
class SyncResult:
    added: list[Transaction] = field(default_factory=list)
    modified: list[Transaction] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    cursor: str | None = None

    def __post_init__(self) -> None:
        for name in ("added", "modified"):
            for txn in getattr(self, name):
                if not isinstance(txn, Transaction):
                    raise TypeError(
                        f"SyncResult.{name} holds Transaction objects, got "
                        f"{type(txn).__name__}"
                    )
        for rid in self.removed:
            if not isinstance(rid, str):
                raise TypeError(
                    f"SyncResult.removed holds ids (str), not objects, got "
                    f"{type(rid).__name__}. A removed row is gone from the "
                    f"source, so there is nothing left to build an object from."
                )


# ---------------------------------------------------------------------------
# Invariant guards
#
# Cheap, and they fail at the boundary where the bad value was introduced
# rather than three layers later in a total that looks merely surprising.
# ---------------------------------------------------------------------------

def _require_int_cents(value: object, field_name: str) -> None:
    """Reject floats outright -- invariant 1.

    `isinstance(True, int)` is True in Python, but a boolean amount is absurd
    enough that excluding it explicitly is worth the extra clause.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{field_name} must be integer minor units, got "
            f"{type(value).__name__}({value!r}). Use core.money.to_cents() -- "
            f"int(amount * 100) is the bug this exists to prevent."
        )


def _clean_currency(currency: str) -> str:
    """Normalize to upper-case ISO-4217, so 'usd' and 'USD' never split a group.

    LENGTH IS NOT ENOUGH. An earlier version checked only `len == 3`, which let
    "U$D" and "1 2" through -- and the failure is not a crash but a currency
    group keyed on garbage, plus `core.money.minor_units` silently falling back
    to 2 decimal places for it.

    ASCII is part of the check, not pedantry: "ÉUR".isalpha() is True, and a
    lookalike code would sit alongside the real one as a separate group that
    reads identically in a table.
    """
    cleaned = currency.strip().upper()
    if len(cleaned) != 3 or not (cleaned.isascii() and cleaned.isalpha()):
        raise ValueError(f"currency must be a 3-letter ISO-4217 code, got {currency!r}")
    return cleaned
