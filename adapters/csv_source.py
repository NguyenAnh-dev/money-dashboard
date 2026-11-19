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
    kind      -> PURCHASE, for every row without exception. See KIND below --
                 this is the one manufactured field where the honest answer is
                 "this source cannot know", and it is a real limitation rather
                 than a placeholder waiting to be filled in.
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

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import NamedTuple

from adapters.fingerprint import ContentIdMinter
from adapters.merchant import MERCHANT_PREFIXES, normalize_merchant  # noqa: F401
from core.models import (
    Account,
    AccountType,
    Provider,
    ConnectionRef,
    SourceRef,
    SyncResult,
    Transaction,
    TransactionKind,
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

#: Every row this adapter emits, and A KNOWN LIMITATION OF THE CSV SOURCE
#: rather than a stub.
#:
#: TransactionKind.TRANSFER needs to know that money moved between two accounts
#: the SAME PERSON owns. A three-column export carries a date, a descriptor and
#: an amount: no category, no counterparty, and no knowledge of any account
#: other than the one being exported. The fact simply is not in the file.
#:
#: The tempting fix is to pattern-match the descriptor -- "AMEX EPAYMENT",
#: "ONLINE TRANSFER TO", "PAYMENT THANK YOU" -- and it is the wrong one, in both
#: directions. A false positive silently REMOVES real spending from the totals
#: (insights.spending_by_category skips TRANSFER), which is the worst failure a
#: money dashboard has: a number that is too small and looks fine. A false
#: negative leaves the 4.7 double-count in place, which at least matches what
#: the row says. And "PAYMENT" appears in the descriptors of plenty of ordinary
#: purchases. Guessing here would trade a visible, explicable overcount for an
#: invisible undercount.
#:
#: So the CONSEQUENCE, stated rather than hidden: a CSV containing a card
#: payment double-counts it in Spending by Category and can have it detected as
#: a subscription -- exactly the three defects the fake fixture demonstrated
#: through 4.7. The real fix is upstream, in a source that categorizes (Plaid
#: does), or in a user-facing override once there is a database to store one.
KIND = TransactionKind.PURCHASE

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


_CURRENCY_MARKER = re.compile(r"[A-Za-z]{3}|[€£¥₹]")
_AMOUNT_SYNTAX = re.compile(
    r"(?P<u1>(?i:usd)\s*)?(?P<d1>\$\s*)?(?P<sign>[+-]\s*)?"
    r"(?P<u2>(?i:usd)\s*)?(?P<d2>\$\s*)?"
    r"(?P<num>\d{1,3}(?:[, \xa0]\d{3})+|\d+)(?P<frac>\.\d+)?"
    r"(?P<u3>\s*(?i:usd))?"
)


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
    if text is None:
        raise ValueError(f"missing {COLUMNS['amount']!r} value")
    trimmed = text.strip()
    foreign = _CURRENCY_MARKER.search(trimmed)
    if foreign and foreign.group().upper() != CURRENCY:
        if _AMOUNT_SYNTAX.fullmatch(_CURRENCY_MARKER.sub("", trimmed).strip()):
            raise ValueError(
                f"unsupported currency {foreign.group()!r} in amount "
                f"{text!r}; only USD is supported"
            )
    match = _AMOUNT_SYNTAX.fullmatch(trimmed)
    if match is None or sum(
        1 for g in ("u1", "u2", "u3") if match.group(g)
    ) > 1 or (match.group("d1") and match.group("d2")):
        raise ValueError(
            f"cannot parse amount {text!r} from the {COLUMNS['amount']!r} "
            f"column: malformed amount syntax"
        )
    number = (match.group("sign") or "").strip() + match.group("num").translate(
        _STRIP_CHARS) + (match.group("frac") or "")
    try:
        cents = to_cents(number, CURRENCY)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"cannot parse amount {text!r} from the {COLUMNS['amount']!r} "
            f"column: {exc}"
        ) from None
    signed = SIGN * cents
    if not _SQLITE_MIN <= signed <= _SQLITE_MAX:
        raise ValueError(
            f"amount {text!r} is outside the supported range for stored cents"
        )
    return signed


# ---------------------------------------------------------------------------
# Step 4: the merchant normalizer
#
# MOVED to adapters/merchant.py at 4.7, unchanged in behaviour. Plaid needs the
# same fallback -- its `merchant_name` is null on about half of real rows -- and
# two adapters sharing a function is the moment it stops belonging to either of
# them. Re-exported under the old private name so callers and tests that import
# `_normalize_merchant` from here keep working.
# ---------------------------------------------------------------------------

_normalize_merchant = normalize_merchant

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


def _ref(provider_id: str, connection_id: str = CONNECTION_ID) -> SourceRef:
    """Provenance for one record of this connection.

    `fetched_at` stays None for the same reason the fixture leaves it None: a
    file on disk was not fetched at a moment, and a timestamp here would be a
    lie that reads as a fact in a debug dump.

    `connection_id` is an argument for the reason adapters/fake.py's is: the
    constant is the same string for every user, so the sync path supplies a
    scoped one instead.
    """
    return SourceRef(
        provider=PROVIDER,
        provider_id=provider_id,
        connection_id=connection_id,
    )


def _ns(provider_id: str, connection_id: str = CONNECTION_ID) -> str:
    """Bare provider id -> our namespaced id. "account" -> "csv:bank_export:account".

    BOTH fetch methods route through this, which is what stops the failure the
    structural gate exists to catch: namespacing the account while transactions
    keep a bare id renders byte-identically and joins to nothing.
    """
    return make_id(_ref(provider_id, connection_id))


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

    `connection_id` defaults to the module constant and is scoped per user by
    the sync path, because one import batch named "bank_export" is the same
    string for everybody -- see docs/USERS.md §6.
    """

    def __init__(self, path: str | None = None, account: CsvAccount = DEFAULT_ACCOUNT,
                 connection_id: str = CONNECTION_ID,
                 rows: Sequence[ParsedRow] | None = None) -> None:
        self._rows = rows
        self.path = path or os.getenv("CSV_PATH", DEFAULT_PATH)
        self.account = account
        self.connection_id = connection_id

    def fetch_accounts(self) -> list[Account]:
        """The one account this statement is a statement of.

        A list of one, not a bare Account, because the contract is the contract:
        every adapter returns a list, and a caller that special-cased CSV would
        be a caller that learned where its data came from.
        """
        return [
            Account(
                id=_ns(self.account.provider_id, self.connection_id),
                source=_ref(self.account.provider_id, self.connection_id),
                name=self.account.name,
                institution=self.account.institution,
                type=self.account.type,
                balance_cents=self.account.balance_cents,
                currency=CURRENCY,
            )
        ]

    def connection(self) -> ConnectionRef:
        return ConnectionRef(provider=PROVIDER, connection_id=self.connection_id)

    def fetch_transactions(self, cursor: str | None = None) -> SyncResult:
        minter = ContentIdMinter("txn")
        account_id = _ns(self.account.provider_id, self.connection_id)
        rows = self._rows if self._rows is not None else read_rows(self.path)

        transactions = [self._build(row, minter, account_id) for row in rows]
        transactions.sort(key=lambda t: t.date)
        return SyncResult(added=transactions)

    def _build(self, row: ParsedRow, minter: ContentIdMinter,
               account_id: str) -> Transaction:
        provider_id = minter.mint(
            row.date.isoformat(), row.description, row.amount_cents,
            self.account.provider_id,
        )

        return Transaction(
            id=_ns(provider_id, self.connection_id),
            source=_ref(provider_id, self.connection_id),
            account_id=account_id,
            date=row.date,
            name=row.description,
            merchant=_normalize_merchant(row.description),
            amount_cents=row.amount_cents,
            category=UNCATEGORIZED,
            currency=CURRENCY,
            pending=False,
            kind=KIND,
        )


class ParsedRow(NamedTuple):
    date: date
    description: str
    amount_cents: int


_LINE_BREAK = re.compile(rb"\r\n|\r|\n")
CURRENCY_COLUMN = "Currency"
_SQLITE_MIN = -(2 ** 63)
_SQLITE_MAX = 2 ** 63 - 1


def _decode(path: str) -> str:
    with open(path, "rb") as fh:
        data = fh.read()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        line = len(_LINE_BREAK.findall(data[:exc.start])) + 1
        raise ValueError(
            f"{path} line {line}: invalid UTF-8 ({exc.reason} at byte "
            f"{exc.start})"
        ) from None


def _records(text: str, path: str):
    i = 0
    n = len(text)
    line = 1

    def bad(where: str, reason: str) -> ValueError:
        return ValueError(f"{path} line {where}: malformed CSV: {reason}")

    while i < n:
        start = line
        fields: list[tuple[str, bool]] = []
        while True:
            if i < n and text[i] == '"':
                i += 1
                buf: list[str] = []
                while True:
                    if i >= n:
                        raise bad(f"{start}-{line}" if line != start
                                  else str(start),
                                  "unterminated quoted field")
                    ch = text[i]
                    if ch == '"':
                        if i + 1 < n and text[i + 1] == '"':
                            buf.append('"')
                            i += 2
                            continue
                        i += 1
                        break
                    if ch == "\r":
                        buf.append(ch)
                        i += 1
                        if i < n and text[i] == "\n":
                            buf.append("\n")
                            i += 1
                        line += 1
                        continue
                    if ch == "\n":
                        line += 1
                    buf.append(ch)
                    i += 1
                if i < n and text[i] not in ",\r\n":
                    raise bad(str(line),
                              "unexpected character after closing quote")
                fields.append(("".join(buf), True))
            else:
                j = i
                while j < n and text[j] not in ",\r\n":
                    if text[j] == '"':
                        raise bad(str(line), "quote inside an unquoted field")
                    j += 1
                fields.append((text[i:j], False))
                i = j
            if i < n and text[i] == ",":
                i += 1
                continue
            break
        if i < n:
            if text[i] == "\r":
                i += 1
                if i < n and text[i] == "\n":
                    i += 1
            else:
                i += 1
            line += 1
        if len(fields) == 1 and fields[0] == ("", False):
            continue
        yield start, [value for value, _ in fields]


def read_rows(path: str) -> list[ParsedRow]:
    rows: list[ParsedRow] = []
    header: list[str] | None = None
    index: dict[str, int] = {}
    currency_at: int | None = None
    for start, record in _records(_decode(path), path):
        if header is None:
            header = record
            _require_columns(header, path)
            index = {name: header.index(col) for name, col in COLUMNS.items()}
            currency_at = _currency_index(header, path)
            continue
        try:
            rows.append(_parse_record(record, header, index, currency_at))
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{path} line {start}: {exc}") from None
    if header is None:
        raise ValueError(f"{path}: file is empty")
    return rows


def _parse_record(record: list[str], header: list[str],
                  index: dict[str, int], currency_at: int | None) -> ParsedRow:
    if len(record) < len(header):
        raise ValueError(
            f"row has {len(record)} field(s), header has {len(header)}: "
            f"missing field(s)"
        )
    if len(record) > len(header):
        raise ValueError(
            f"row has more fields than the header: surplus "
            f"{record[len(header):]!r}. Usually an unquoted thousands "
            f"separator in the {COLUMNS['amount']!r} column, which would "
            f"silently truncate the amount to its leading digits."
        )
    if currency_at is not None:
        declared = record[currency_at].strip()
        if declared and declared.upper() != CURRENCY:
            raise ValueError(
                f"unsupported currency {declared!r} in the "
                f"{CURRENCY_COLUMN!r} column; only USD is supported"
            )
    when = datetime.strptime(record[index["date"]].strip(), DATE_FORMAT).date()
    return ParsedRow(
        when, record[index["description"]], clean_amount(record[index["amount"]])
    )


def _currency_index(header: list[str], path: str) -> int | None:
    found = [i for i, name in enumerate(header)
             if name.strip().casefold() == CURRENCY_COLUMN.casefold()]
    if len(found) > 1:
        names = [header[i] for i in found]
        raise ValueError(
            f"{path} line 1: duplicate column(s) {names} "
            f"(compared ignoring case and surrounding spaces)"
        )
    return found[0] if found else None


def _require_columns(fieldnames: list[str] | None, path: str) -> None:
    present = set(fieldnames or ())
    missing = [column for column in COLUMNS.values() if column not in present]
    if missing:
        raise ValueError(
            f"{path}: missing column(s) {missing}. Found {sorted(present)}. "
            f"Adjust COLUMNS in adapters/csv_source.py for this bank's export."
        )
    watched = list(COLUMNS.values())
    duplicated = sorted({c for c in watched if (fieldnames or []).count(c) > 1})
    if duplicated:
        raise ValueError(f"{path} line 1: duplicate column(s) {duplicated}")
