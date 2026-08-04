"""
adapters/plaid_source.py
------------------------
Plaid, as an ADAPTER. Same contract as fake.py and csv_source.py:
`fetch_accounts()` / `fetch_transactions()` returning core.models types, and
nothing above this layer learns the data came over a network.

THE FILE IS SPLIT IN TWO, AND THAT IS THE WHOLE DESIGN:

  * TRANSLATION -- `to_account` / `to_transaction`, module-level functions that
    take a plain dict and return a model. No network, no credentials, no class.
  * I/O -- `PlaidSource`, which fetches dicts and hands them to the above.

Every invariant the conformance suite checks is a property of the translation
half. Making it a pure function of a dict means `fixtures/plaid_sandbox.json`
-- a real recorded sandbox payload -- can exercise all of it on every run, on
any machine, with no credentials. The alternative (assertions that only run
when someone exports a token) is a contract that is enforced approximately
never, which is the same as no contract.

WHAT PLAID GIVES YOU THAT A CSV DOES NOT, and what it takes away:

    id        -> ITS OWN. `transaction_id` / `account_id` are stable, so this
                 adapter never touches adapters/fingerprint.py. See the note on
                 `pending_transaction_id` below -- Plaid's stability guarantee
                 is different from ours, not merely stronger.
    merchant  -> `merchant_name`, WHEN IT IS NOT NULL. It is null on about half
                 of real rows, so the raw `name` still goes through the shared
                 normalizer. This is not an edge case; it is the common path.
    category  -> `personal_finance_category.primary`, a real taxonomy rather
                 than the CSV's "Uncategorized".
    balance   -> a real balance, not a placeholder zero.
    amounts   -> FLOATS. `110.0`, `6.33`. Every one of them, on both accounts
                 and transactions. See `_to_cents` for why that is the most
                 dangerous thing on this list.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal

from adapters.merchant import normalize_merchant
from core.models import (
    DEBT_TYPES,
    Account,
    AccountType,
    Provider,
    SourceRef,
    Transaction,
    TransactionKind,
    make_id,
)
from core.money import minor_units, to_cents

#: This adapter's provider. First segment of every id it mints.
PROVIDER = Provider.PLAID

#: Plaid's type/subtype pair -> our AccountType.
#:
#: Keyed on (type, subtype) rather than type alone because the distinction that
#: matters to us lives in the subtype: a `depository` is a CHECKING or a SAVINGS
#: and the difference is not cosmetic once someone computes a savings rate.
#:
#: Plaid publishes dozens of combinations and adds more; this table is the ONE
#: place that knows about them, which is exactly what AccountType exists for.
ACCOUNT_TYPES: dict[tuple[str, str], AccountType] = {
    ("depository", "checking"):        AccountType.CHECKING,
    ("depository", "savings"):         AccountType.SAVINGS,
    ("depository", "cd"):              AccountType.SAVINGS,
    ("depository", "money market"):    AccountType.SAVINGS,
    ("depository", "hsa"):             AccountType.SAVINGS,
    ("depository", "cash management"): AccountType.CHECKING,
    ("depository", "prepaid"):         AccountType.CHECKING,
    ("credit", "credit card"):         AccountType.CREDIT,
    ("credit", "paypal"):              AccountType.CREDIT,
    ("loan", "student"):               AccountType.LOAN,
    ("loan", "mortgage"):              AccountType.LOAN,
    ("loan", "auto"):                  AccountType.LOAN,
    ("loan", "home equity"):           AccountType.LOAN,
    ("loan", "line of credit"):        AccountType.LOAN,
    ("investment", "ira"):             AccountType.INVESTMENT,
    ("investment", "401k"):            AccountType.INVESTMENT,
    ("investment", "brokerage"):       AccountType.INVESTMENT,
    ("investment", "roth"):            AccountType.INVESTMENT,
    ("investment", "529"):             AccountType.INVESTMENT,
}

#: Falls back on the bare `type` when the exact pair is unknown, so a subtype
#: Plaid adds next quarter still lands in the right family instead of OTHER.
ACCOUNT_TYPE_FAMILIES: dict[str, AccountType] = {
    "depository": AccountType.CHECKING,
    "credit":     AccountType.CREDIT,
    "loan":       AccountType.LOAN,
    "investment": AccountType.INVESTMENT,
    "brokerage":  AccountType.INVESTMENT,
}

#: `personal_finance_category.primary` -> our TransactionKind.
#:
#: KEYED ON PLAID'S RAW CONSTANTS, not on what `category_of` renders them into.
#: That function turns "TRANSFER_OUT" into the display string "Transfer Out",
#: and keying off THAT would make this table depend on a title-casing rule that
#: exists purely for how a table looks -- change `category_of` to drop
#: underscores differently and transfer handling would break silently, with a
#: card payment quietly rejoining the spending total. The raw constants are
#: Plaid's published enum; the display string is ours to restyle.
#:
#: LOAN_PAYMENTS IS A JUDGMENT CALL, not a mechanical mapping, and it is the one
#: line here that will get a bug report. The other four are obvious.
#:
#: What makes it defensible is what the bucket actually CONTAINS. Plaid files
#: CREDIT_CARD_PAYMENT under LOAN_PAYMENTS, so the primary is dominated by card
#: payments -- precisely the Amex double-count TRANSFER exists to stop, where the
#: purchases already counted when they posted. In our own recorded payload the
#: split is 100% card payments and zero mortgages:
#:
#:     LOAN_PAYMENTS_CREDIT_CARD_PAYMENT   "CREDIT CARD 3333 PAYMENT"   x3
#:     LOAN_PAYMENTS_OTHER_PAYMENT         "AUTOMATIC PAYMENT - THANK"  x3
#:
#: THE CONTESTED CASE IS A MORTGAGE, and it is genuinely different from a card
#: payment. Its principal is net-worth-neutral (the asset drops, the liability
#: drops with it), but its INTEREST is a real expense -- money that leaves and
#: never comes back, no different from a fee. Plaid sends ONE amount and no
#: amortization split, so the two cannot be separated here: it is one bucket or
#: nothing.
#:
#: TRANSFER is the bucket, and NOT because principal usually dominates -- early
#: in a mortgage term the opposite is true and interest is most of the payment.
#: The reason is the asymmetry in what each error costs. Mapping this to PURCHASE
#: restores the card-payment double-count for the MAJORITY of the bucket, which
#: is the exact defect 4.8 exists to remove, and it inflates a spending table for
#: everyone. Mapping it to TRANSFER understates spending only for someone paying
#: a loan we cannot see, by the interest portion of it.
#:
#: THE VISIBLE SYMPTOM, so nobody has to rediscover it: a mortgage or car payment
#: DISAPPEARS from Spending by Category. That is the intended behaviour of this
#: line, not a bug -- but it is surprising enough to be worth the paragraph.
#:
#: The escape hatch exists and is deliberately not taken. `detailed` distinguishes
#: LOAN_PAYMENTS_MORTGAGE_PAYMENT / _CAR_PAYMENT / _STUDENT_LOAN_PAYMENT from
#: _CREDIT_CARD_PAYMENT, so this one primary could be keyed on `detailed` the day
#: it matters. It buys nothing today (the fixture has no mortgage) and it would
#: still not solve the real problem, which is that the interest portion belongs in
#: spending and no field in the payload says how much of the amount it is.
#:
#: Anything not listed is PURCHASE, matching Transaction's own default. That
#: means a primary Plaid adds next quarter lands in the spending total rather
#: than vanishing from it, which is the direction to fail in: an unclassified
#: charge showing up as ordinary spending is a small inaccuracy, one silently
#: dropped is money the dashboard never mentions.
KINDS: dict[str, TransactionKind] = {
    "TRANSFER_IN":    TransactionKind.TRANSFER,
    "TRANSFER_OUT":   TransactionKind.TRANSFER,
    "LOAN_PAYMENTS":  TransactionKind.TRANSFER,
    "INCOME":         TransactionKind.INCOME,
    "BANK_FEES":      TransactionKind.FEE,
}

#: What a transaction with no `personal_finance_category` becomes. Same choice
#: the CSV adapter makes: the provider did not say, and inventing one from the
#: descriptor is a guess dressed as a fact.
UNCATEGORIZED = "Uncategorized"

#: Plaid's default when an account reports no currency. Applied ONLY when both
#: iso_currency_code and unofficial_currency_code are absent -- see
#: `_currency_of`, which refuses to invent a code over a real non-ISO one.
DEFAULT_CURRENCY = "USD"

#: What goes in `Account.institution` for every Plaid account, and it is a
#: PLACEHOLDER -- the accounts_get response does not carry the bank's name.
#:
#: The consequence is visible: a Plaid render reports "1 institutions" in the
#: footer, and the net-worth panel's "across all banks" is aspirational.
#:
#: Getting the real name costs a second API call -- /institutions/get_by_id,
#: keyed on `item.institution_id` from /item/get -- which buys one footer line
#: in exchange for another network round trip and another failure mode. Not
#: taken deliberately: this is COSMETICALLY wrong, not DISHONESTLY wrong, since
#: nothing claims the name is real. (Contrast the header, which said "fake
#: data" over live balances and was fixed at 4.7 for exactly that reason.)
#:
#: There is deliberately NO plumbing standing by for it. An unpopulated seam
#: reads as a wired feature, and whoever implements this will pass the name to
#: `to_account` as an explicit argument, which is clearer than a slot that has
#: been quietly empty for months.
INSTITUTION_NAME = "Plaid"


# ---------------------------------------------------------------------------
# Field coercion
#
# Three of these guard failures that produce a rendered dashboard full of
# plausible wrong numbers rather than a crash.
# ---------------------------------------------------------------------------

def _to_cents(amount, currency: str) -> int:
    """A Plaid float in major units -> integer minor units.

    THIS IS THE MOST DANGEROUS FUNCTION IN THE FILE. Plaid sends every amount
    as a JSON number, which the SDK hands over as a Python float: 6.33, 110.0.
    `core.money.to_cents` REFUSES floats outright, and that refusal is the
    point -- `int(6.33 * 100)` is 632, not 633, because 6.33 is really
    6.32999999999999996447286321199499070644378662109375.

    The fix is to stop being a float before any arithmetic happens. `str()` on
    a float gives the shortest decimal string that round-trips to the same
    float (repr semantics since 3.1), so a value that arrived as "6.33" in the
    JSON becomes exactly "6.33" again -- and `to_cents` then does the scaling
    in Decimal, where 6.33 * 100 is 633 exactly.

    An int is passed through untouched: to_cents already reads a bare int as
    whole major units, which is what Plaid means by an amount of `5`.
    """
    if isinstance(amount, bool):
        # isinstance(True, int) is True, so the int branch would take it.
        raise TypeError(f"amount is a bool ({amount!r}), not money")
    if isinstance(amount, float):
        # Not int(amount * 100). Never int(amount * 100).
        return to_cents(str(amount), currency)
    return to_cents(amount, currency)


def _balance_to_cents(amount, currency: str) -> int:
    """Like `_to_cents`, but ROUNDS -- and only balances may use it.

    Found by recording a real sandbox payload rather than reading the schema:
    the 401k account reports `current: 23631.9805`. Four decimal places, which
    `to_cents` refuses outright, so a strict conversion fails the entire sync
    on an ordinary retirement account.

    Rounding is correct HERE and wrong for a transaction, and the asymmetry is
    the point:

      * A balance is a snapshot of a market value that already moved while the
        response was in flight. Its sub-cent tail is noise, and `Account.as_of`
        exists because the whole number is an approximation.
      * A transaction amount is what somebody was actually charged. A fraction
        of a cent there is not noise, it is a bug in the payload or in our
        reading of it, and it should stop the import rather than be smoothed
        away. Zero of the 48 recorded transactions have one.

    `core.money.to_cents` refuses to round and says the caller that wants
    rounding should say so at the call site. This is that call site, said out
    loud, in one function that only balances reach.

    ROUND_HALF_EVEN because it is Decimal's default and unbiased over many
    values; the choice is immaterial at this magnitude but should not be an
    accident.
    """
    if isinstance(amount, bool):
        raise TypeError(f"balance is a bool ({amount!r}), not money")

    value = Decimal(str(amount))
    exponent = Decimal(1).scaleb(-minor_units(currency))
    return to_cents(str(value.quantize(exponent, rounding=ROUND_HALF_EVEN)),
                    currency)


def _to_date(value) -> date:
    """Whatever the payload holds -> a real `date`.

    Three inputs, because there are genuinely three:
      * `date`     -- what plaid-python deserializes the `date` field into.
      * `str`      -- what the same field looks like after `to_dict()` and a
                      JSON round-trip, which is how the recorded fixture stores
                      it. The test fixture and the live SDK disagree on type,
                      and pretending otherwise means the tests exercise a code
                      path production never runs.
      * `datetime` -- Plaid also sends `datetime` and `authorized_datetime`.
                      Nothing here should read those, but `datetime` IS a
                      `date` subclass, so one slipping through would pass every
                      isinstance check and then carry a time component that
                      perturbs every day-gap in find_recurring.

    The datetime case is narrowed to its date rather than rejected: the caller
    asked for the day something happened, and that is unambiguous.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"cannot read a date from {type(value).__name__}({value!r})")


def _currency_of(fields: dict) -> str:
    """ISO-4217 code, read from whichever dict actually carries it.

    THE ARGUMENT IS NOT ALWAYS THE RECORD, and the two call sites differ:

      * a transaction  -> the TOP-LEVEL dict; Plaid puts iso_currency_code
                          beside `amount`.
      * an account     -> its `balances` SUB-dict; Plaid puts the code next to
                          the numbers it denominates, not on the account.

    Hence `fields` rather than `record`: this takes "the dict that holds the
    currency pair", and passing an account's outer dict would silently find
    neither key and fall through to DEFAULT_CURRENCY. Naming it honestly is
    the guard, since there is no way to tell the two shapes apart at runtime.

    `unofficial_currency_code` is checked and then REFUSED rather than ignored.
    It is how Plaid reports crypto and other non-ISO denominations, and quietly
    substituting "USD" for one would put a Bitcoin balance into the net-worth
    total as dollars. `core.models` requires a real ISO code, so this fails at
    the record that has one instead of inventing a number nobody can trace.
    """
    iso = fields.get("iso_currency_code")
    if iso:
        return iso

    unofficial = fields.get("unofficial_currency_code")
    if unofficial:
        raise ValueError(
            f"account/transaction is denominated in {unofficial!r}, which is "
            f"not ISO-4217. Substituting a default here would put a non-dollar "
            f"balance into a dollar total."
        )
    return DEFAULT_CURRENCY


def account_type_of(raw: dict) -> AccountType:
    """Plaid's (type, subtype) -> ours, never raising.

    Unknown combinations fall back: exact pair, then the bare `type` family,
    then OTHER. Deliberately not an error -- Plaid adds subtypes on its own
    schedule, and an account we cannot classify precisely is still an account
    whose balance belongs in the total. Failing the whole sync over a subtype
    string would be a worse outcome than showing it as OTHER.

    Note that landing in OTHER has a consequence worth knowing: OTHER is not in
    DEBT_TYPES, so a NEW liability subtype would be treated as an asset and its
    balance would not be flipped.
    """
    plaid_type = (raw.get("type") or "").strip().lower()
    subtype = (raw.get("subtype") or "").strip().lower()

    if (plaid_type, subtype) in ACCOUNT_TYPES:
        return ACCOUNT_TYPES[(plaid_type, subtype)]
    return ACCOUNT_TYPE_FAMILIES.get(plaid_type, AccountType.OTHER)


def category_of(raw: dict) -> str:
    """`personal_finance_category.primary` -> a display string.

    "TRANSPORTATION" -> "Transportation", "FOOD_AND_DRINK" -> "Food And Drink".

    `primary` rather than `detailed`: detailed is
    TRANSPORTATION_TAXIS_AND_RIDE_SHARES, which is accurate and useless as a
    spending-table row. The legacy `category` list is deliberately ignored --
    Plaid deprecated it, and it is a hierarchy in an array, which is a shape
    this app has nowhere to put.

    THIS FUNCTION LOOKS LIKE IT SOLVES THE TRANSFER PROBLEM AND DOES NOT, which
    is what 4.8 acted on. Plaid does tag transfers -- TRANSFER_IN and
    TRANSFER_OUT -- but they arrive here as the strings "Transfer In" and
    "Transfer Out", while the fake fixture writes the literal "Transfer" and a
    CSV writes "Uncategorized". Three sources, three spellings of one idea, so
    keying behaviour off the category STRING works on the fixture and quietly
    fails on the other two. That is why `kind` is a real enum field each adapter
    sets deliberately -- see `kind_of` below, and note it reads Plaid's RAW
    constants rather than the display text this function returns.
    """
    pfc = raw.get("personal_finance_category") or {}
    primary = (pfc.get("primary") or "").strip()
    if not primary:
        return UNCATEGORIZED
    return primary.replace("_", " ").title()


def kind_of(raw: dict) -> TransactionKind:
    """`personal_finance_category.primary` -> TransactionKind, never raising.

    Reads the RAW constant, not `category_of`'s output -- see KINDS for why
    that distinction is the whole point of this function existing separately.

    Unknown and absent both become PURCHASE, deliberately without distinguishing
    them. A row Plaid did not categorize and a row categorized into something we
    have not mapped are the same thing to us: an ordinary charge until told
    otherwise. TransactionKind.PURCHASE is also the model's default, so the two
    halves of "we could not classify this" agree.
    """
    pfc = raw.get("personal_finance_category") or {}
    primary = (pfc.get("primary") or "").strip().upper()
    return KINDS.get(primary, TransactionKind.PURCHASE)


def merchant_of(raw: dict) -> str:
    """`merchant_name`, or the raw descriptor through the shared normalizer.

    NULL ON ABOUT HALF OF REAL ROWS -- 24 of 48 in the recorded sandbox
    payload. An adapter that maps `merchant_name` straight through violates the
    never-empty invariant on half its output, and the symptom is not a crash:
    it is one enormous group keyed on None that swallows those rows in every
    total the dashboard computes.
    """
    name = raw.get("merchant_name")
    if name and name.strip():
        return name.strip()
    return normalize_merchant(raw.get("name") or "")


# ---------------------------------------------------------------------------
# Translation: dict -> model. Pure, so the recorded fixture can drive it.
# ---------------------------------------------------------------------------

def _ref(provider_id: str, item_id: str) -> SourceRef:
    """Provenance for one record of one Item.

    `connection_id` is the ITEM ID, which is what a Plaid connection actually
    is: one login at one institution. Re-linking the same bank produces a new
    Item and therefore new ids, which is correct -- it is a different grant,
    and the old access_token stops working.
    """
    return SourceRef(
        provider=PROVIDER,
        provider_id=provider_id,
        connection_id=item_id,
    )


def to_account(raw: dict, item_id: str) -> Account:
    """One Plaid account dict -> Account.

    THE DEBT FLIP LIVES HERE, and it is the assertion armed since 4.2. Plaid
    reports a credit card's balance as POSITIVE debt: `current: 410.0` means
    you owe $410. Our model says debt is negative. Get this backwards and net
    worth is off by TWICE the card balance, while every individual number on
    screen still looks entirely reasonable.

    `tests/test_conformance.py::test_debt_accounts_are_not_positive` is what
    catches it, immediately, which is the entire reason that check was written
    before this adapter existed.
    """
    balances = raw.get("balances") or {}
    currency = _currency_of(balances)
    account_type = account_type_of(raw)

    current = balances.get("current")
    if current is None:
        # Plaid can omit `current` for some investment accounts. Zero is the
        # honest placeholder -- the same choice CsvAccount makes -- because
        # inventing one from `available` would render as a real balance.
        balance_cents = 0
    else:
        balance_cents = _balance_to_cents(current, currency)

    if account_type in DEBT_TYPES:
        balance_cents = -balance_cents

    available = balances.get("available")
    available_cents = (
        None if available is None else _balance_to_cents(available, currency)
    )

    provider_id = raw["account_id"]
    return Account(
        id=make_id(_ref(provider_id, item_id)),
        source=_ref(provider_id, item_id),
        name=raw.get("name") or provider_id,
        # HARDCODED, and known to be wrong. See INSTITUTION_NAME.
        institution=INSTITUTION_NAME,
        type=account_type,
        balance_cents=balance_cents,
        currency=currency,
        mask=raw.get("mask"),
        available_cents=available_cents,
    )


def to_transaction(raw: dict, item_id: str) -> Transaction:
    """One Plaid transaction dict -> Transaction.

    NO SIGN FLIP. Plaid already uses our convention -- positive means money
    left the account -- which makes this the one adapter where the amount is
    passed through as-is. `csv_source.SIGN = -1` exists because an OFX-style
    export disagrees; writing a flip here out of symmetry would invert every
    row, and the symptom is silent: spending and income simply swap places.

    IDS ARE PLAID'S OWN. `transaction_id` is stable and globally unique, so
    nothing here goes near adapters/fingerprint.py -- the content hash exists
    precisely for sources that supply no id.

    But Plaid's stability guarantee is NOT ours, and the difference matters at
    Step 5: when a pending charge settles, Plaid issues a NEW transaction_id
    and reports the old one in `pending_transaction_id`. Our content-hash
    scheme keeps the id and lets the upsert update the row; Plaid's scheme
    hands us two ids for one charge and expects the consumer to reconcile them.
    Neither is wrong, but code that assumes ours will duplicate every settling
    Plaid charge. The Step 5 reconciler needs `pending_transaction_id`, and it
    reads it out of `raw` -- which holds the WHOLE payload, because that is
    what `Transaction.raw` is documented to be ("the provider's original
    payload"). Stashing a single hand-picked key under that name would read as
    the full response to anyone who trusts the model's docstring.

    The cost is memory: a few KB per row, excluded from `repr` so it never
    lands in a log line by accident. The thing to know before Step 5 is that
    this dict is NOT JSON-serializable as it stands -- the live SDK puts real
    `date` objects in it -- so persisting it needs a serializer, not a dump.
    """
    currency = _currency_of(raw)
    provider_id = raw["transaction_id"]

    return Transaction(
        id=make_id(_ref(provider_id, item_id)),
        source=_ref(provider_id, item_id),
        account_id=make_id(_ref(raw["account_id"], item_id)),
        date=_to_date(raw["date"]),
        name=raw.get("name") or "",
        merchant=merchant_of(raw),
        amount_cents=_to_cents(raw["amount"], currency),
        category=category_of(raw),
        currency=currency,
        pending=bool(raw.get("pending", False)),
        kind=kind_of(raw),
        # COPIED, not aliased. `raw=raw` would hand the model the caller's own
        # dict: RecordedPlaidSource passes rows straight out of a module-scoped
        # JSON payload, so a single `txn.raw[k] = v` anywhere downstream would
        # rewrite the fixture for every later reader in the process.
        #
        # Shallow is the right depth. It stops the realistic failure -- adding
        # or removing a top-level key -- at the cost of one dict per row.
        # Nested values (`location`, `payment_meta`,
        # `personal_finance_category`) are still shared, so this is a guard
        # against accidental annotation, not an immutability guarantee. A deep
        # copy would cost real memory per row to defend against a mutation
        # nobody has a reason to make.
        raw=dict(raw),
    )


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

class PlaidSource:
    """A linked Plaid Item as a source.

    Constructor takes an access token rather than reading the environment, so
    the object is testable and two Items can coexist in one process. Use
    `from_env()` for the configured one.
    """

    def __init__(self, access_token: str, item_id: str | None = None,
                 client=None) -> None:
        if not access_token:
            raise ValueError(
                "PlaidSource needs an access_token. Run "
                "scripts/get_plaid_token.py once and put the result in .env "
                "as PLAID_ACCESS_TOKEN."
            )
        self._access_token = access_token
        self._item_id = item_id
        self._client = client

    @classmethod
    def from_env(cls) -> PlaidSource:
        """Build from PLAID_* variables. Called by data_source.py.

        PLAID_ITEM_ID IS REQUIRED HERE, though the constructor treats it as
        optional, and the asymmetry is deliberate.

        `_connection_id()` can resolve a missing Item id from /item/get and
        caches it on the instance -- which is fine for a script or a test that
        holds one adapter. Since 5.7 sync.py holds one too, so the cache does
        now work there and the cost of an unset PLAID_ITEM_ID is one extra
        round trip per sync rather than two per render.

        Still refused, because one avoidable network call on the only command
        that touches the network is worth a one-line config fix. The value is
        not a secret and scripts/get_plaid_token.py prints it next to the token.
        """
        item_id = os.environ.get("PLAID_ITEM_ID", "").strip()
        if not item_id:
            raise ValueError(
                "PLAID_ITEM_ID is not set. Add it to .env:\n"
                "  PLAID_ITEM_ID=<the item_id printed by "
                "scripts/get_plaid_token.py>\n"
                "It is not a secret. Without it every render makes two extra "
                "/item/get calls, because data_source builds a new adapter per "
                "call and the resolved id cannot be cached across them."
            )
        return cls(
            access_token=os.environ.get("PLAID_ACCESS_TOKEN", "").strip(),
            item_id=item_id,
        )

    # -- plumbing ---------------------------------------------------------

    def _api(self):
        """The Plaid client, built on first use.

        Imported INSIDE the method, not at module scope. `data_source` already
        imports this adapter lazily so `DATA_SOURCE=fake` never loads the SDK;
        deferring one more level means the TRANSLATION half of this file can be
        imported -- by the conformance tests, by the fixture-backed tests --
        without plaid-python being involved at all.
        """
        if self._client is None:
            import plaid
            from plaid.api import plaid_api

            env = os.environ.get("PLAID_ENV", "sandbox").strip().lower()
            if env != "sandbox":
                raise ValueError(
                    f"PLAID_ENV={env!r}: this project only runs against "
                    f"sandbox. Production credentials read real accounts."
                )

            configuration = plaid.Configuration(
                host=plaid.Environment.Sandbox,
                api_key={
                    "clientId": os.environ.get("PLAID_CLIENT_ID", "").strip(),
                    "secret": os.environ.get("PLAID_SECRET", "").strip(),
                },
            )
            self._client = plaid_api.PlaidApi(plaid.ApiClient(configuration))
        return self._client

    def _connection_id(self) -> str:
        """The Item id, fetched once if it was not supplied.

        Resolved lazily rather than in __init__ so constructing a PlaidSource
        makes no network call -- which is what lets the conformance suite build
        one and then skip cleanly when credentials are absent.

        THE CACHE ONLY HELPS A LONG-LIVED INSTANCE. `data_source.get_accounts()`
        and `get_transactions()` each construct a new adapter, so a caller going
        through them resolves this once per fetch and never carries it over.
        `data_source.get_source()` exists so sync.py can hold one instance and
        get the cache; `from_env()` still refuses to build without
        PLAID_ITEM_ID, and direct construction keeps the lazy path for scripts
        and tests.
        """
        if self._item_id:
            return self._item_id

        from plaid.model.item_get_request import ItemGetRequest

        response = self._api().item_get(ItemGetRequest(
            access_token=self._access_token))
        self._item_id = response["item"]["item_id"]
        return self._item_id

    # -- the contract -----------------------------------------------------

    def fetch_accounts(self) -> list[Account]:
        from plaid.model.accounts_get_request import AccountsGetRequest

        item_id = self._connection_id()
        response = self._api().accounts_get(
            AccountsGetRequest(access_token=self._access_token))

        return [to_account(account.to_dict(), item_id)
                for account in response["accounts"]]

    def fetch_transactions(self) -> list[Transaction]:
        """Every transaction, following the sync cursor to the end.

        `has_more` is the ONLY correct stop condition. An empty page is not the
        end and neither is a short one -- /transactions/sync paginates on its
        own schedule, and stopping early silently truncates the statement.

        Sorted by date on the way out, matching what the other two adapters
        hand back, so `test_two_fetches_produce_identical_ids` compares a
        stable order rather than whatever order the pages happened to arrive in.
        """
        from plaid.model.transactions_sync_request import TransactionsSyncRequest

        item_id = self._connection_id()
        added, cursor = [], None
        while True:
            # Cursor passed to the CONSTRUCTOR, not assigned afterwards.
            # Post-construction assignment does work on plaid-python 42 (these
            # are generated OpenAPI models and `cursor` is in the schema), but
            # the constructor is the documented form and does not depend on
            # that. Worth stating because this branch is easy to leave
            # unverified: the sandbox Item returns 48 transactions against a
            # count of 500, so `has_more` is false on the first page and the
            # second iteration never happens.
            kwargs = {"access_token": self._access_token, "count": 500}
            if cursor is not None:
                kwargs["cursor"] = cursor
            response = self._api().transactions_sync(
                TransactionsSyncRequest(**kwargs))
            added.extend(response["added"])
            cursor = response["next_cursor"]
            if not response["has_more"]:
                break

        transactions = [to_transaction(t.to_dict(), item_id) for t in added]
        transactions.sort(key=lambda t: (t.date, t.id))
        return transactions
