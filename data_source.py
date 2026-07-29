"""
data_source.py
--------------
The DATA layer of the app.

Right now this returns hardcoded fake data. In Step 4, you will add a second
function `get_plaid_data()` that talks to the real Plaid sandbox API and
returns data in EXACTLY this same shape. Nothing else in the app will need
to change. That is the whole point of building inside-out.

KEY RULE: money is stored as INTEGER CENTS, never as a float.
  $12.34  ->  1234
Floats lose precision (0.1 + 0.2 != 0.3 in Python), and in fintech that is
how you silently lose money. Say this in an interview and people notice.
"""

from dataclasses import dataclass
from datetime import date, timedelta

#MY FORMAT, NOT PLAID'S ONE

@dataclass
class Account:
    account_id: str
    name: str          # e.g. "Chase Checking"
    institution: str   # e.g. "Chase"
    balance_cents: int # current balance, in cents


@dataclass
class Transaction:
    transaction_id: str
    account_id: str      # which account it belongs to
    date: date
    name: str            # RAW descriptor, exactly as the bank sends it
                         #   e.g. "AMZN Mktp US*2X4B1QR03"
    merchant: str        # CLEANED merchant name, e.g. "Amazon"
    amount_cents: int    # POSITIVE = money spent, NEGATIVE = money received
    category: str        # e.g. "Food & Drink"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def dollars(cents: int) -> str:
    """Turn integer cents into a display string. 1234 -> '$12.34'"""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"


def _posts_on(year: int, month: int, anchor_day: int) -> date:
    """A subscription billed on the 4th does NOT always post on the 4th.

    Card networks and ACH do not settle on weekends, so a charge anchored to
    a Saturday shows up on the following Monday. This is why a real recurring
    detector must match on a DAY WINDOW (say, anchor +/- 3) instead of an
    exact day number.
    """
    d = date(year, month, anchor_day)
    while d.weekday() >= 5:          # 5 = Saturday, 6 = Sunday
        d += timedelta(days=1)
    return d


def get_accounts() -> list[Account]:
    return [
        Account("acc_1", "Chase Checking",   "Chase",          142387),
        Account("acc_2", "Amex Gold Card",   "American Express", -87215),
        Account("acc_3", "Ally Savings",     "Ally",           520000),
    ]


def get_transactions() -> list[Transaction]:
    """Fake transactions spanning May-July 2026, shaped like a real statement.

    WHAT YOUR STEP-2 DETECTOR SHOULD FIND (true recurring):
      - Spotify         $10.99  anchored to the 4th   (posts late in July)
      - Netflix         $15.49  anchored to the 12th  (PRICE RISES to $17.99)
      - Planet Fitness  $24.99  anchored to the 20th  (posts late in June)
      - T-Mobile        ~$84    anchored to the 16th  (AMOUNT VARIES - hard case)
      - Payroll Deposit incoming, monthly on the 1st

    WHAT IT SHOULD NOT FLAG (real-life noise):
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
    """
    t = []

    def add(when, name, merchant, amount_cents, category, account="acc_1"):
        t.append(Transaction("", account, when, name, merchant,
                             amount_cents, category))

    # -----------------------------------------------------------------
    # RECURRING -- these are the signal your detector must find
    # -----------------------------------------------------------------
    tmobile_cents = {5: 8431, 6: 8431, 7: 9177}   # July had a data overage

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

        # --- income ---
        add(date(2026, month, 1), "ACME CORP DIR DEP PPD ID:1234567890",
            "Payroll Deposit", -230000, "Income", "acc_1")

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
    add(date(2026, 7, 29), "CVS/PHARM 04412 Q10",        "CVS Pharmacy",   987, "Health",       "acc_1")
    add(date(2026, 7, 31), "STARBUCKS STORE 08842",      "Starbucks",      545, "Food & Drink", "acc_2")

    # Statements arrive in date order, so hand them back that way and number
    # them afterwards.
    t.sort(key=lambda txn: txn.date)
    for i, txn in enumerate(t, start=1):
        txn.transaction_id = f"txn_{i:03d}"
    return t
