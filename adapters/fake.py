"""
adapters/fake.py
----------------
The built-in fixture, as an ADAPTER. It owns the hardcoded statement that
data_source.py used to hold, and it is the reference implementation of the
adapter contract: translate a provider's records INTO core.models, and let
nothing above this layer learn where the data came from.

CONNECTION IDENTITY
Every adapter names the connection its records arrived through, because that
is the middle segment of every id it mints (see core.models.make_id):

    fake:fixture:acc_1        fake:fixture:txn_001

The two constants below are what feed that. They are module-level and NAMED
rather than inlined at each make_id call, for three reasons:

  1. The pair is the pattern, and every future adapter needs its own. The CSV
     adapter's connection_id is one import batch; Plaid's is an Item id, which
     arrives at runtime from the link exchange rather than being a literal.
     Naming them here is what makes that shape obvious to copy.
  2. An id is a primary key. Typing "fixture" in five places is five chances
     to type "fixtures" once, and the result is not a crash -- it is a second
     account that silently fails to match the first on the next sync.
  3. Changing a connection_id REKEYS every row this adapter has ever emitted.
     That is a decision worth making at one visible line, not by editing a
     string literal buried in a loop.
"""

from datetime import date, timedelta

from adapters.fingerprint import ContentIdMinter
from core.models import (
    Account,
    AccountType,
    Provider,
    SourceRef,
    Transaction,
    TransactionKind,
    make_id,
)

#: This adapter's provider. First segment of every id it mints.
PROVIDER = Provider.FAKE

#: Middle segment: which linked connection these records came through. There is
#: exactly one fixture and it never re-links, so unlike a Plaid Item id this can
#: be a constant. Never empty -- make_id rejects a blank segment outright.
CONNECTION_ID = "fixture"


def _ref(provider_id: str) -> SourceRef:
    """Provenance for one record of this connection.

    `fetched_at` stays None: the fixture is not fetched from anywhere, and a
    timestamp here would be a lie that looks like a fact in a debug dump.
    """
    return SourceRef(
        provider=PROVIDER,
        provider_id=provider_id,
        connection_id=CONNECTION_ID,
    )


def _ns(provider_id: str) -> str:
    """Bare provider id -> our namespaced id. "acc_1" -> "fake:fixture:acc_1".

    BOTH fetch methods route through this, and that is the entire point. The
    failure it prevents is the one the structural gate exists for: namespace
    the accounts, leave `Transaction.account_id` holding a bare "acc_1", and
    every join silently resolves to nothing while the dashboard -- which never
    joins -- renders byte-identically. One function means the two sides cannot
    drift, because there is only one spelling of the id to get wrong.
    """
    return make_id(_ref(provider_id))


def _posts_on(year: int, month: int, anchor_day: int) -> date:
    """A subscription billed on the 4th does NOT always post on the 4th.

    Card networks and ACH do not settle on weekends, so a charge anchored to
    a Saturday shows up on the following Monday. This is why a real recurring
    detector must match on a DAY WINDOW (say, anchor +/- 3) instead of an
    exact day number.

    Lives here rather than in data_source.py because it is fixture-GENERATION
    logic: it invents plausible dates for made-up data. A real adapter has no
    equivalent -- Plaid sends the date a charge actually posted.
    """
    d = date(year, month, anchor_day)
    while d.weekday() >= 5:          # 5 = Saturday, 6 = Sunday
        d += timedelta(days=1)
    return d


class FakeSource:
    """The fixture as a source. Same contract every other adapter implements:
    `fetch_accounts()` and `fetch_transactions()`, returning core.models types.

    Stateless, so the constructor takes nothing. Plaid's will take an access
    token and an Item id -- that asymmetry is exactly why the contract is a
    class rather than two module-level functions.
    """

    def fetch_accounts(self) -> list[Account]:
        """Three accounts at three institutions, one of them a credit card.

        `type` is required by the model, and it is not decoration -- it is what
        tells the rest of the system that Amex's balance is DEBT. The fixture
        already carries -87215, i.e. already negative, because our model says
        debt is negative. A provider that reports card balances as positive
        (Plaid does) needs that flip in ITS adapter, not here.
        """
        rows = [
            # provider_id, name,            institution,        type,                 balance
            ("acc_1", "Chase Checking", "Chase",            AccountType.CHECKING, 142387),
            ("acc_2", "Amex Gold Card", "American Express", AccountType.CREDIT,   -87215),
            ("acc_3", "Ally Savings",   "Ally",             AccountType.SAVINGS,  520000),
        ]
        return [
            Account(
                id=_ns(provider_id),
                source=_ref(provider_id),
                name=name,
                institution=institution,
                type=account_type,
                balance_cents=balance_cents,
                currency="USD",
            )
            for provider_id, name, institution, account_type, balance_cents in rows
        ]

    def fetch_transactions(self) -> list[Transaction]:
        """Fake transactions spanning May-July 2026, shaped like a real statement.

        WHAT MY STEP-3 DETECTOR SHOULD DO (true recurring):
          - Spotify         $10.99  anchored to the 4th   (posts late in July)
          - Netflix         $15.49  anchored to the 12th  (PRICE RISES to $17.99)
          - Planet Fitness  $24.99  anchored to the 20th  (posts late in June)
          - T-Mobile        ~$84    anchored to the 16th  (AMOUNT VARIES - hard case)

        WHAT IT SHOULD NOT FLAG (real-life noise):
          - Payroll Deposit. It IS perfectly monthly (the 1st, gaps of 31 and 30
            days), but the detector is charges-only BY DESIGN: it skips
            `amount_cents < 0` before grouping. "Recurring" here means
            subscriptions -- a paycheck sorted by `latest_cents` lands below
            Spotify, and a price-change delta is meaningless against a raise.
            Recurring income is a separate feature (cash-flow), not this one.
          - Starbucks, Chipotle, Whole Foods, Trader Joes, Uber, Amazon, CVS.
            These repeat, but irregularly: the count per month varies, the
            amounts vary, the days do not line up, and some merchants skip a
            month entirely. That is what discretionary spending actually looks
            like -- nobody buys Chipotle on the 3rd for $14.42 every month.

        TRAPS DELIBERATELY BAKED IN:
          1. Chipotle hits $14.42 in all three months, on three different days.
             An amount-only matcher will flag it. It is not a subscription.
          2. Amazon's raw descriptor carries a different order code every time,
             so naive exact-string grouping sees ~8 separate merchants. You must
             normalize `name` -> `merchant` before grouping. This is the single
             biggest thing that will break when you swap in real Plaid data.
          3. Uber trips and Uber Eats share a merchant prefix but land in
             different categories.
          4. A PENDING Starbucks on 2026-07-30. find_recurring skips it (a
             pending amount is provisional, and including it fabricates the
             price change this detector exists to report); every other view
             counts it, so it is in the spending total and the count.

        THE AMEX PAYMENT IS A TRANSFER, AND 4.8 IS WHERE IT SAYS SO:
          Money leaves acc_1 (checking) on the 25th of all three months to pay
          the acc_2 card. The amounts VARY DELIBERATELY -- $512.80, $439.65,
          $682.04 -- because you pay what you spent, and that variance was the
          sharpest way to see the problem it used to cause: a "+$242.39"
          price-rise alert on a card payment. A flat amount would have hidden
          that behind an empty Change column.

          Through 4.7 the row carried three visible defects, all of them
          downstream of ONE missing fact -- the model had no way to say "this is
          a move between two accounts I own", so every rule the detector had was
          satisfied honestly:

            - detected as a FIFTH SUBSCRIPTION (monthly, one charge per month,
              gaps of 31 and 32 days);
            - $1634.49 of DOUBLE-COUNTED SPENDING, since the Amex purchases
              already counted when they were charged -- enough to outrank
              Groceries as the top category;
            - a MEANINGLESS price-change alert on a card payment.

          `kind=TransactionKind.TRANSFER` supplies that fact, and all three go
          away at once: find_recurring and spending_by_category both skip
          TRANSFER. THE ROW STAYS. It is still in the transaction count and
          still in the recent list, because the payment did happen -- deleting
          it would discard the demonstration rather than fix it, and it is the
          only row proving the skip works end to end.
        """
        # Ids are derived from each row's CONTENT, not its position -- see
        # adapters/fingerprint.py for why txn_001.. is a trap. The practical
        # consequence here: inserting a transaction into the middle of this
        # fixture changes exactly one id instead of renumbering everything
        # below it, and reordering these lines changes none at all.
        minter = ContentIdMinter("txn")

        rows: list[tuple] = []

        def add(when, name, merchant, amount_cents, category, account="acc_1",
                pending=False, kind=TransactionKind.PURCHASE):
            # The RAW descriptor and the bare provider account id go into the
            # hash; `merchant`, `category`, `pending` and `kind` stay out of it
            # deliberately. The first two are OUR derived values, and the day
            # the merchant cleaner improves is not the day every historical id
            # should move. `pending` is worse: it flips to False on settle, so
            # hashing it would rekey the row exactly when it posts -- turning
            # the Step 5 upsert into a duplicate. `kind` is the same argument as
            # `category`: it is a classification we assign, so reclassifying a
            # row must not rekey it.
            provider_id = minter.mint(when.isoformat(), name, amount_cents, account)
            rows.append((provider_id, when, name, merchant, amount_cents,
                         category, _ns(account), pending, kind))

        # -----------------------------------------------------------------
        # RECURRING -- these are the signal your detector must find
        # -----------------------------------------------------------------
        tmobile_cents = {5: 8431, 6: 8431, 7: 9177}   # July had a data overage

        # You pay what you spent, so a card payment is never the same twice.
        # That variance is the demonstration -- see the docstring.
        amex_payment_cents = {5: 51280, 6: 43965, 7: 68204}

        for month in (5, 6, 7):
            add(_posts_on(2026, month, 4), "SPOTIFY P0A1B2C3",
                "Spotify", 1099, "Entertainment", "acc_2")

            # Netflix quietly raises its price in July -- a great alert feature later
            add(_posts_on(2026, month, 12), "NETFLIX.COM 866-579-7172 CA",
                "Netflix", 1549 if month < 7 else 1799, "Entertainment", "acc_2")

            add(_posts_on(2026, month, 20), "PLANET FIT CLUB FEES 8776874652",
                "Planet Fitness", 2499, "Health", "acc_1")

            # Same merchant, same cadence, DIFFERENT amount each month.
            add(_posts_on(2026, month, 16), "T-MOBILE PCS SVC 800-937-8997",
                "T-Mobile", tmobile_cents[month], "Bills & Utilities", "acc_1")

            # A TRANSFER, and as of 4.8 it SAYS SO -- see the docstring. The
            # `kind` is what analysis reads; the "Transfer" category string
            # beside it is display text that Plaid and a CSV spell differently.
            # Anchored to the 25th, clear of the 4/12/16/20 anchors above so no
            # two recurring rows share a billing day.
            add(_posts_on(2026, month, 25), "AMEX EPAYMENT ACH PMT 1234",
                "Amex Payment", amex_payment_cents[month], "Transfer", "acc_1",
                kind=TransactionKind.TRANSFER)

            # --- income: monthly, but NOT a target of find_recurring (see docstring) ---
            # INCOME is set for honesty, not for effect: find_recurring already
            # skips this row on `amount_cents < 0`, and nothing reads the kind
            # yet. It is the sign that carries the meaning today.
            add(date(2026, month, 1), "ACME CORP DIR DEP PPD ID:1234567890",
                "Payroll Deposit", -230000, "Income", "acc_1",
                kind=TransactionKind.INCOME)

        # -----------------------------------------------------------------
        # MAY -- discretionary noise
        # -----------------------------------------------------------------
        add(date(2026, 5, 2),  "WHOLEFDS MKT #10259",        "Whole Foods",   8214, "Groceries",    "acc_1")
        add(date(2026, 5, 4),  "STARBUCKS STORE 08842",      "Starbucks",      545, "Food & Drink", "acc_2")
        add(date(2026, 5, 5),  "AMZN Mktp US*2X4B1QR03",     "Amazon",        4599, "Shopping",     "acc_2")
        add(date(2026, 5, 6),  "STARBUCKS STORE 08842",      "Starbucks",      675, "Food & Drink", "acc_2")
        add(date(2026, 5, 8),  "CHIPOTLE 2094",              "Chipotle",      1442, "Food & Drink", "acc_2")
        add(date(2026, 5, 9),  "TRADER JOE'S #412 QPS",      "Trader Joes",   4388, "Groceries",    "acc_1")
        add(date(2026, 5, 12), "SQ *STARBUCKS RESERVE",      "Starbucks",      485, "Food & Drink", "acc_2")
        add(date(2026, 5, 15), "UBER   *TRIP HELP.UBER.COM", "Uber",          2310, "Transport",    "acc_2")
        add(date(2026, 5, 16), "TST* SHAKE SHACK - 442",     "Shake Shack",   2640, "Food & Drink", "acc_2")
        add(date(2026, 5, 17), "WHOLEFDS MKT #10259",        "Whole Foods",   6109, "Groceries",    "acc_1")
        add(date(2026, 5, 18), "Amazon.com*M12AB3CD4",       "Amazon",        2314, "Shopping",     "acc_2")
        add(date(2026, 5, 20), "CHIPOTLE ONLINE 800244768",  "Chipotle",      1215, "Food & Drink", "acc_2")
        add(date(2026, 5, 21), "STARBUCKS STORE 08842",      "Starbucks",      675, "Food & Drink", "acc_2")
        add(date(2026, 5, 23), "UBER   *TRIP HELP.UBER.COM", "Uber",          1865, "Transport",    "acc_2")
        add(date(2026, 5, 24), "UBER   *EATS HELP.UBER.COM", "Uber Eats",     3122, "Food & Drink", "acc_2")
        add(date(2026, 5, 26), "CVS/PHARM 04412 Q10",        "CVS Pharmacy",  1820, "Health",       "acc_1")
        add(date(2026, 5, 27), "AMZN Mktp US*9K3P7TE21",     "Amazon",        1299, "Shopping",     "acc_2")
        add(date(2026, 5, 29), "STARBUCKS STORE 08842",      "Starbucks",      720, "Food & Drink", "acc_2")
        add(date(2026, 5, 30), "WHOLEFDS MKT #10259",        "Whole Foods",  10477, "Groceries",    "acc_1")

        # -----------------------------------------------------------------
        # JUNE -- note: no CVS and no Shake Shack at all this month
        # -----------------------------------------------------------------
        add(date(2026, 6, 2),  "SQ *STARBUCKS RESERVE",      "Starbucks",      485, "Food & Drink", "acc_2")
        add(date(2026, 6, 4),  "CHIPOTLE 2094",              "Chipotle",      1587, "Food & Drink", "acc_2")
        add(date(2026, 6, 5),  "UBER   *TRIP HELP.UBER.COM", "Uber",          1420, "Transport",    "acc_2")
        add(date(2026, 6, 6),  "WHOLEFDS MKT #10259",        "Whole Foods",   9103, "Groceries",    "acc_1")
        add(date(2026, 6, 8),  "AMZN Mktp US*4C8N2WQ77",     "Amazon",        6742, "Shopping",     "acc_2")
        add(date(2026, 6, 11), "STARBUCKS STORE 08842",      "Starbucks",      675, "Food & Drink", "acc_2")
        add(date(2026, 6, 13), "TRADER JOE'S #412 QPS",      "Trader Joes",   3755, "Groceries",    "acc_1")
        add(date(2026, 6, 13), "UBER   *EATS HELP.UBER.COM", "Uber Eats",     2875, "Food & Drink", "acc_2")
        add(date(2026, 6, 15), "STARBUCKS STORE 08842",      "Starbucks",      545, "Food & Drink", "acc_2")
        add(date(2026, 6, 18), "AMAZON.COM*XR51JD9B2",       "Amazon",         999, "Shopping",     "acc_2")
        add(date(2026, 6, 19), "STARBUCKS STORE 08842",      "Starbucks",      675, "Food & Drink", "acc_2")
        add(date(2026, 6, 20), "UBER   *TRIP HELP.UBER.COM", "Uber",          2795, "Transport",    "acc_2")
        add(date(2026, 6, 21), "WHOLEFDS MKT #10259",        "Whole Foods",   5842, "Groceries",    "acc_1")
        add(date(2026, 6, 23), "CHIPOTLE 2094",              "Chipotle",      1442, "Food & Drink", "acc_2")
        add(date(2026, 6, 24), "UBER   *TRIP HELP.UBER.COM", "Uber",          1640, "Transport",    "acc_2")
        add(date(2026, 6, 28), "TRADER JOE'S #412 QPS",      "Trader Joes",   5231, "Groceries",    "acc_1")

        # -----------------------------------------------------------------
        # JULY -- busiest month, includes a one-off trip
        # -----------------------------------------------------------------
        add(date(2026, 7, 1),  "STARBUCKS STORE 08842",      "Starbucks",      675, "Food & Drink", "acc_2")
        add(date(2026, 7, 2),  "CHIPOTLE 2094",              "Chipotle",      1328, "Food & Drink", "acc_2")
        add(date(2026, 7, 6),  "CVS/PHARM 04412 Q10",        "CVS Pharmacy",  2241, "Health",       "acc_1")
        add(date(2026, 7, 8),  "AMZN Mktp US*7B2V4XG09",     "Amazon",       11244, "Shopping",     "acc_2")
        add(date(2026, 7, 9),  "DELTA AIR 0062348871234",    "Delta Air Lines", 34200, "Travel",    "acc_2")
        add(date(2026, 7, 9),  "UBER   *TRIP HELP.UBER.COM", "Uber",          4280, "Transport",    "acc_2")
        add(date(2026, 7, 10), "STARBUCKS STORE 08842",      "Starbucks",      545, "Food & Drink", "acc_2")
        add(date(2026, 7, 11), "WHOLEFDS MKT #10259",        "Whole Foods",   7760, "Groceries",    "acc_1")
        add(date(2026, 7, 12), "UBER   *TRIP HELP.UBER.COM", "Uber",          2135, "Transport",    "acc_2")
        add(date(2026, 7, 13), "STARBUCKS STORE 08842",      "Starbucks",      720, "Food & Drink", "acc_2")
        add(date(2026, 7, 14), "Amazon.com*P83QZ1LM6",       "Amazon",        3420, "Shopping",     "acc_2")
        add(date(2026, 7, 16), "SQ *STARBUCKS RESERVE",      "Starbucks",      485, "Food & Drink", "acc_2")
        add(date(2026, 7, 17), "CHIPOTLE 2094",              "Chipotle",      1442, "Food & Drink", "acc_2")
        add(date(2026, 7, 18), "UBER   *EATS HELP.UBER.COM", "Uber Eats",     2460, "Food & Drink", "acc_2")
        add(date(2026, 7, 21), "TRADER JOE'S #412 QPS",      "Trader Joes",   6218, "Groceries",    "acc_1")
        add(date(2026, 7, 23), "AMZN Mktp US*2X4B1QR03",     "Amazon",        4599, "Shopping",     "acc_2")
        add(date(2026, 7, 24), "TST* SHAKE SHACK - 442",     "Shake Shack",   1885, "Food & Drink", "acc_2")
        add(date(2026, 7, 25), "WHOLEFDS MKT #10259",        "Whole Foods",   8819, "Groceries",    "acc_1")
        add(date(2026, 7, 27), "CHIPOTLE 2094",              "Chipotle",      1694, "Food & Drink", "acc_2")
        add(date(2026, 7, 28), "STARBUCKS STORE 08842",      "Starbucks",      675, "Food & Drink", "acc_2")
        # Still authorized, not settled. The amount can still move, which is why
        # find_recurring skips it; everything else counts it as real money.
        add(date(2026, 7, 30), "STARBUCKS STORE 08842",      "Starbucks",      675, "Food & Drink", "acc_2", pending=True)
        add(date(2026, 7, 29), "CVS/PHARM 04412 Q10",        "CVS Pharmacy",   987, "Health",       "acc_1")
        add(date(2026, 7, 31), "STARBUCKS STORE 08842",      "Starbucks",      545, "Food & Drink", "acc_2")

        # Statements arrive in date order, so hand them back that way. `sort` is
        # stable, so rows sharing a date keep insertion order. Note that this is
        # now presentation only: the ids were minted from content above and do
        # not depend on where a row lands in this list.
        rows.sort(key=lambda row: row[1])
        return [
            Transaction(
                id=_ns(provider_id),
                source=_ref(provider_id),
                account_id=account_id,
                date=when,
                name=name,
                merchant=merchant,
                amount_cents=amount_cents,
                category=category,
                currency="USD",
                pending=pending,
                kind=kind,
            )
            for provider_id, when, name, merchant, amount_cents, category,
            account_id, pending, kind in rows
        ]
