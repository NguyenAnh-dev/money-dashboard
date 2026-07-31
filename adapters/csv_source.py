"""
adapters/csv_source.py
----------------------
A bank's exported statement, as an ADAPTER. Same contract as adapters/fake.py:
`fetch_accounts()` / `fetch_transactions()` returning core.models types, and
nothing above this layer learns the data arrived as a CSV.

WHAT A CSV EXPORT ACTUALLY GIVES YOU
Three columns and nothing else: a date, a descriptor, an amount. No id, no
merchant, no category, no currency, no account. Every one of those has to be
MANUFACTURED here, and each is a decision this file has to own:

    id        -> minted from row CONTENT (adapters/fingerprint.py), never from
                 row position, because inserting one row must not rekey the
                 rest of the file.
    merchant  -> normalized from the descriptor (`_normalize_merchant`), because
                 the model forbids an empty one and grouping happens on it.
    category  -> UNCATEGORIZED. The bank did not say, and inventing one from the
                 descriptor is a guess dressed as a fact.
    currency  -> the CURRENCY constant. A statement is denominated in one.
    account   -> supplied to the constructor. The file does not know which
                 account it is a statement OF; the person exporting it does.

THE CROSSWALK IS A SET OF NAMED CONSTANTS, not literals sprinkled through the
parse loop. Every bank's export differs in exactly these five ways, so the next
bank is a copy of this file with the constants changed -- and, more to the
point, SIGN is visible at the top of the file where a reviewer can check it
against a real statement instead of inferring it from a `-` buried in a loop.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime

from adapters.fingerprint import ContentIdMinter
from core.models import (
    Account,
    AccountType,
    Provider,
    SourceRef,
    Transaction,
    make_id,
)
from core.money import to_cents

# ---------------------------------------------------------------------------
# The crosswalk: this bank's dialect, in five constants
# ---------------------------------------------------------------------------

#: Our field -> this export's column header. The headers are matched EXACTLY as
#: the bank writes them (csv.DictReader keys off the first row), so a bank that
#: ships "Transaction Date" is a one-line change here rather than a rewrite.
COLUMNS = {
    "date": "Date",
    "description": "Description",
    "amount": "Amount",
}

#: Multiplier applied AFTER parsing, to reach our sign convention.
#:
#: Our model says POSITIVE = money left the account (core/models.py invariant 2).
#: This export follows the OFX convention every bank UI uses: a debit is shown
#: negative, the way it looks on the statement. So every amount flips.
#:
#: This is the single most dangerous constant in the file. Get it wrong and
#: nothing crashes -- spending and income swap places, "Spending by Category"
#: silently reports your paycheck, and net worth moves by twice the total. It
#: is a named constant with its own test (tests/test_csv_source.py) for exactly
#: that reason: an inverted sign has no symptom a render can show you.
SIGN = -1

#: strptime pattern for the Date column. US format, so 03/04 is March 4th.
#: A bank exporting DD/MM/YYYY needs "%d/%m/%Y" here, and the two are
#: indistinguishable for the first twelve days of every month -- which is why
#: this is a constant to be set deliberately per bank rather than sniffed.
DATE_FORMAT = "%m/%d/%Y"

#: A statement is denominated in one currency, and the file never says which.
CURRENCY = "USD"

#: This adapter's provider. First segment of every id it mints. It is what
#: makes a CSV id incapable of colliding with a fixture id even if both hash
#: the same content -- "csv:..." and "fake:..." are different strings.
PROVIDER = Provider.CSV

#: Middle segment: which connection these records arrived through. One import
#: batch. A constant here because there is one statement file; a real importer
#: handling several uploads would mint one of these per upload, and changing it
#: REKEYS every row this adapter has ever emitted.
CONNECTION_ID = "bank_export"

#: Where the statement lives when CSV_PATH is unset. Relative to the project
#: root, which is the directory the app is run from.
DEFAULT_PATH = "fixtures/bank_export.csv"

#: The bank did not tell us a category, so we do not claim one. A single honest
#: bucket beats guessing from the descriptor: "SHELL OIL" is a gas station most
#: of the time and a lunch the rest of it.
UNCATEGORIZED = "Uncategorized"

#: Key csv.DictReader parks surplus fields under. It defaults to None, which is
#: a value you cannot distinguish from "this key was absent" -- see
#: `_require_no_extra_fields` for the 1000x error this exists to catch.
_EXTRA = "__extra__"


# ---------------------------------------------------------------------------
# Step 3: the amount cleaner
#
# Split out and tested on its own BEFORE anything calls it. It is two lines of
# string work guarding the one conversion in the system where money can vanish,
# and both of its failure modes are silent: a mis-stripped separator turns
# $1,234.56 into $1.23, and a mis-applied SIGN inverts every row in the file.
# ---------------------------------------------------------------------------

#: Decoration this export wraps around the number. Stripped, not parsed --
#: core.money.to_cents deliberately rejects all of it, so that one CSV dialect's
#: conventions never leak into every future adapter.
_STRIP_CHARS = str.maketrans("", "", "$, \t ")


def clean_amount(text: str) -> int:
    """One Amount cell -> integer minor units in OUR sign convention.

        "-$17.99"    -> 1799     (a debit; positive because money left)
        "$2,300.00"  -> -230000  (a credit; negative because money arrived)
        "-1,204.77"  -> 120477
        "0.00"       -> 0

    Three steps, in this order and no other:

      1. STRIP the presentation -- currency symbol, thousands separators, and
         whitespace (including the non-breaking space some exports emit).
      2. to_cents, which does the decimal arithmetic in Decimal and refuses to
         round. `int(float(text) * 100)` is the bug this avoids: it turns
         "10.99" into 1098.
      3. SIGN, applied LAST. Flipping before the parse would mean hand-editing
         the string, and flipping inside to_cents would put a bank's convention
         inside a shared money primitive.

    What is deliberately NOT handled: "(10.99)" for a negative, "10.99 CR"
    suffixes, and comma decimal separators ("1.234,56"). Those are different
    dialects, and each one silently misreads under this parser -- so they must
    raise here and be added as an explicit branch, not be absorbed by a
    permissive strip.
    """
    stripped = text.translate(_STRIP_CHARS)
    try:
        cents = to_cents(stripped, CURRENCY)
    except (ValueError, TypeError) as exc:
        # Re-raise with the ORIGINAL cell in the message. By this point the
        # value has been mangled by the strip, and "malformed amount '(10.99)'"
        # tells you which row to look at in a way "'(10.99)'" after stripping
        # would not.
        raise ValueError(
            f"cannot parse amount {text!r} from the {COLUMNS['amount']!r} "
            f"column: {exc}"
        ) from None
    return SIGN * cents


# ---------------------------------------------------------------------------
# Step 4: the merchant normalizer
#
# Deliberately dumb. It is a lookup table plus a two-word fallback, and that is
# the whole design: a clever normalizer (regex stripping of store numbers, city
# names, phone numbers) is a rabbit hole that produces confidently wrong names
# and, worse, one that CHANGES -- and merchant is a grouping key. Improving it
# must never move a historical id, which is why fingerprint.mint() is fed the
# raw descriptor and never this function's output.
# ---------------------------------------------------------------------------

#: Descriptor prefix -> merchant name. Matched case-insensitively against the
#: START of the raw descriptor, longest prefix first, so a more specific entry
#: can override a shorter one.
#:
#: Prefixes, not exact matches, because the tail of a descriptor is where banks
#: put the noise: a store number, a city, a phone number, an order code. The
#: head is the stable part.
MERCHANT_PREFIXES = {
    "HULU": "Hulu",
    "SAFEWAY": "Safeway",
    "SHELL OIL": "Shell",
    "COSTCO WHSE": "Costco",
    "DOORDASH*": "DoorDash",
    "HOME DEPOT": "Home Depot",
    "REI ": "REI",
    "ACME CORP PAYROLL": "Payroll Deposit",
}


def _normalize_merchant(description: str) -> str:
    """Raw descriptor -> a merchant name that is never empty.

        "HULU 877-8244858 CA"      -> "Hulu"      (prefix table)
        "PANERA BREAD #601234"     -> "Panera Bread"  (fallback)
        "TARGET"                   -> "Target"        (fallback, one word)

    The fallback is FIRST TWO WORDS, TITLE-CASED, and it is chosen because it
    is predictable rather than because it is good. Two words captures
    "Home Depot" and "Panera Bread" while dropping the store number that would
    split one merchant into eight groups. It gets "Rei #011" wrong, which is
    why REI is in the table -- the table is where accuracy lives, and the
    fallback only has to be non-empty and stable.

    `.title()` is not applied blindly to the table entries: "REI" would become
    "Rei". Table values are used verbatim.
    """
    text = description.strip()
    if not text:
        # An empty merchant violates a model invariant, and Transaction would
        # not catch it (only currency and amount are guarded). Fail at the row
        # that caused it rather than three layers up in a group keyed on "".
        raise ValueError(
            "blank Description: cannot derive a merchant, and the model "
            "forbids an empty one because grouping happens on it"
        )

    upper = text.upper()
    # Longest first: "SHELL OIL" must win over a hypothetical "SHELL".
    for prefix in sorted(MERCHANT_PREFIXES, key=len, reverse=True):
        if upper.startswith(prefix.upper()):
            return MERCHANT_PREFIXES[prefix]

    return " ".join(text.split()[:2]).title()


# ---------------------------------------------------------------------------
# Identity: account + provenance
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CsvAccount:
    """Who this statement belongs to -- supplied, because the file cannot say.

    A CSV export has no account row. The person who downloaded it knows it is
    their Chase checking account; the bytes do not. Passing this to the
    constructor keeps the invention visible at the call site instead of buried
    as a literal in fetch_accounts().

    `balance_cents` defaults to 0 rather than to a guess. A statement is a list
    of transactions, not a balance, and summing the rows would produce the
    period's NET CHANGE while looking exactly like a balance -- a wrong number
    that renders perfectly. Zero is visibly a placeholder.
    """
    provider_id: str = "account"
    name: str = "Bank Export"
    institution: str = "Imported Statement"
    type: AccountType = AccountType.CHECKING
    balance_cents: int = 0


#: Used when the caller does not supply one. Named so the default is a thing
#: you can import and compare against, not an inline literal.
DEFAULT_ACCOUNT = CsvAccount()


def _ref(provider_id: str) -> SourceRef:
    """Provenance for one record of this connection.

    `fetched_at` stays None for the same reason the fixture leaves it None: a
    file on disk was not fetched at a moment, and a timestamp here would be a
    lie that reads as a fact in a debug dump.
    """
    return SourceRef(
        provider=PROVIDER,
        provider_id=provider_id,
        connection_id=CONNECTION_ID,
    )


def _ns(provider_id: str) -> str:
    """Bare provider id -> our namespaced id. "account" -> "csv:bank_export:account".

    BOTH fetch methods route through this, which is what stops the failure the
    structural gate exists to catch: namespacing the account while transactions
    keep a bare id renders byte-identically and joins to nothing.
    """
    return make_id(_ref(provider_id))


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

class CsvSource:
    """A bank CSV export as a source.

    `path` resolves at CONSTRUCTION from CSV_PATH, matching data_source.py's
    rule that configuration is read when it is used rather than frozen at
    import time -- a module-level `PATH = os.getenv(...)` would make the env var
    untestable for anything that imports this module early.

    `account` is the identity of the statement (see CsvAccount).
    """

    def __init__(self, path: str | None = None, account: CsvAccount = DEFAULT_ACCOUNT) -> None:
        self.path = path or os.getenv("CSV_PATH", DEFAULT_PATH)
        self.account = account

    def fetch_accounts(self) -> list[Account]:
        """The one account this statement is a statement of.

        A list of one, not a bare Account, because the contract is the contract:
        every adapter returns a list, and a caller that special-cased CSV would
        be a caller that learned where its data came from.
        """
        return [
            Account(
                id=_ns(self.account.provider_id),
                source=_ref(self.account.provider_id),
                name=self.account.name,
                institution=self.account.institution,
                type=self.account.type,
                balance_cents=self.account.balance_cents,
                currency=CURRENCY,
            )
        ]

    def fetch_transactions(self) -> list[Transaction]:
        """Parse every row of the export into a Transaction.

        One minter per fetch (see adapters/fingerprint.ContentIdMinter): its
        duplicate counter must start clean on every call, or ids would depend
        on how many times the adapter had been called in this process.
        """
        minter = ContentIdMinter("txn")
        account_id = _ns(self.account.provider_id)

        transactions: list[Transaction] = []
        # newline="" is required by the csv module, not optional style: without
        # it, a quoted field containing a newline is split into two rows on
        # Windows. Encoding is pinned to utf-8-sig because bank exports very
        # often carry a BOM, and without `-sig` the first header becomes
        # "﻿Date" and every COLUMNS lookup raises KeyError on row one.
        with open(self.path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh, restkey=_EXTRA)
            _require_columns(reader.fieldnames, self.path)

            for line_no, row in enumerate(reader, start=2):  # 1 is the header
                try:
                    _require_no_extra_fields(row)
                    txn = self._build(row, minter, account_id)
                except (ValueError, KeyError, TypeError) as exc:
                    # Name the file and line. A statement is hundreds of rows,
                    # and "malformed amount" with no location is a search.
                    raise ValueError(
                        f"{self.path} line {line_no}: {exc}"
                    ) from None
                transactions.append(txn)

        # Statements arrive in date order and callers expect that, but the sort
        # is PRESENTATION only -- every id above was minted from content, so
        # reordering this list changes none of them.
        transactions.sort(key=lambda t: t.date)
        return transactions

    def _build(self, row: dict, minter: ContentIdMinter, account_id: str) -> Transaction:
        raw_date = row[COLUMNS["date"]]
        description = row[COLUMNS["description"]]
        raw_amount = row[COLUMNS["amount"]]

        # .strip() on the date because a trailing space is a strptime error
        # with a famously unhelpful message.
        when = datetime.strptime(raw_date.strip(), DATE_FORMAT).date()
        amount_cents = clean_amount(raw_amount)

        # What goes into the hash: the date, the RAW descriptor, the amount in
        # our units, and the account. Not the normalized merchant -- that is
        # our derived value, and improving MERCHANT_PREFIXES tomorrow must not
        # rekey every row minted today. Not the raw amount STRING either: the
        # cents are the fact, so a re-export that writes "17.99" instead of
        # "$17.99" still matches the row already stored.
        provider_id = minter.mint(
            when.isoformat(), description, amount_cents, self.account.provider_id
        )

        return Transaction(
            id=_ns(provider_id),
            source=_ref(provider_id),
            account_id=account_id,
            date=when,
            name=description,
            merchant=_normalize_merchant(description),
            amount_cents=amount_cents,
            category=UNCATEGORIZED,
            currency=CURRENCY,
            # A CSV export is settled history. Pending charges are, by
            # definition, not on the statement you downloaded.
            pending=False,
        )


def _require_columns(fieldnames: list[str] | None, path: str) -> None:
    """Fail on the header, not on row 400.

    csv.DictReader returns None for a missing column rather than raising, so a
    renamed header would otherwise surface as a TypeError deep inside strptime
    with no hint that the header is what changed.
    """
    present = set(fieldnames or ())
    missing = [column for column in COLUMNS.values() if column not in present]
    if missing:
        raise ValueError(
            f"{path}: missing column(s) {missing}. Found {sorted(present)}. "
            f"Adjust COLUMNS in adapters/csv_source.py for this bank's export."
        )


def _require_no_extra_fields(row: dict) -> None:
    """A row with more fields than the header is a 1000x error, not a warning.

    The way this happens in practice: the export writes a thousands separator
    into an UNQUOTED amount, so `...,COSTCO,-$1,204.77` parses as four fields.
    Amount becomes "-$1" and "204.77" lands in the surplus bucket. Nothing
    raises -- `clean_amount("-$1")` is a perfectly valid 100 cents -- and a
    $1,204.77 charge renders as $1.00 on a dashboard that looks entirely
    healthy. It is precisely the class of failure a render cannot show you, so
    it has to fail here.
    """
    if row.get(_EXTRA):
        raise ValueError(
            f"row has more fields than the header: surplus {row[_EXTRA]!r}. "
            f"Usually an unquoted thousands separator in the "
            f"{COLUMNS['amount']!r} column, which would silently truncate the "
            f"amount to its leading digits."
        )
