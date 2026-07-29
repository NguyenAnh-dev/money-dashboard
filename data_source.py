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
from datetime import date

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
    merchant: str        # e.g. "Spotify"
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

def get_accounts() -> list[Account]:
    return [
        Account("acc_1", "Chase Checking",   "Chase",          142387),
        Account("acc_2", "Amex Gold Card",   "American Express", -87215),
        Account("acc_3", "Ally Savings",     "Ally",           520000),
    ]


def get_transactions() -> list[Transaction]:
    """Fake transactions spanning ~3 months.

    Note the deliberate patterns baked in here -- these exist so that your
    recurring-charge detector (Step 2) has something real to find:
      - Spotify   $10.99  on the 4th of each month
      - Netflix   $15.49  on the 12th of each month  (PRICE RISES in month 3)
      - Planet Fitness $24.99 on the 20th of each month
      - plus noisy one-off purchases that should NOT be flagged
    """
    t = []
    n = 0

    def add(y, m, d, merchant, amount_cents, category, account="acc_1"):
        nonlocal n
        n += 1
        t.append(Transaction(f"txn_{n:03d}", account, date(y, m, d),
                             merchant, amount_cents, category))

    for month in (5, 6, 7):
        # --- recurring subscriptions (your detector should find these) ---
        add(2026, month, 4,  "Spotify",         1099, "Entertainment", "acc_2")
        add(2026, month, 20, "Planet Fitness",  2499, "Health",        "acc_1")
        # Netflix quietly raises its price in July -- a great alert feature later
        netflix = 1549 if month < 7 else 1799
        add(2026, month, 12, "Netflix",      netflix, "Entertainment", "acc_2")

        # --- income ---
        add(2026, month, 1, "Payroll Deposit", -230000, "Income", "acc_1")

        # --- noisy one-offs (should NOT be flagged as recurring) ---
        add(2026, month, 3,  "Chipotle",        1442, "Food & Drink", "acc_2")
        add(2026, month, 7,  "Whole Foods",     8734, "Groceries",    "acc_1")
        add(2026, month, 15, "Uber",            2310, "Transport",    "acc_2")
        add(2026, month, 18, "Starbucks",        675, "Food & Drink", "acc_2")
        add(2026, month, 23, "Amazon",          4599, "Shopping",     "acc_2")
        add(2026, month, 26, "CVS Pharmacy",    1820, "Health",       "acc_1")

    # a few extras only in July, to make the latest month look less uniform
    add(2026, 7, 9,  "Delta Air Lines", 34200, "Travel",       "acc_2")
    add(2026, 7, 21, "Trader Joes",      6218, "Groceries",    "acc_1")
    add(2026, 7, 24, "Shake Shack",      1885, "Food & Drink", "acc_2")

    return t
