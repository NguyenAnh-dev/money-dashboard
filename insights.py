from dataclasses import dataclass
from calendar import monthrange
from datetime import date, timedelta

from core.models import TransactionKind

MIN_MONTHS = 3
MIN_GAP_DAYS = 26
MAX_GAP_DAYS = 35


@dataclass
class RecurringCharge:
    merchant: str
    account_id: str
    latest_cents: int           # what it costs now
    previous_cents: int | None  # last amount that DIFFERS from latest;
                                # None means the price has never moved
    day: int                    # billing anchor: earliest day-of-month seen
    last_date: date
    occurrences: int
    evidence: tuple = ()        # ((date, cents), ...) oldest first, for review

    # Derived, so the CLI and (later) the API report a price change the same
    # way instead of each recomputing it in its own display loop.
    @property
    def delta_cents(self) -> int | None:
        """Signed price move, or None if the price has never changed."""
        if self.previous_cents is None:
            return None
        return self.latest_cents - self.previous_cents

    @property
    def direction(self) -> str | None:
        """'up', 'down', or None if the price has never changed."""
        delta = self.delta_cents
        if not delta:
            return None
        return "up" if delta > 0 else "down"

def total_balance(accounts):
    total = 0
    for account in accounts:
        total += account.balance_cents
    return total

def spending_by_category(transactions):
    result = {}
    for txn in transactions:
        # A transfer is not spending. Paying the Amex card moves money between
        # two accounts the user owns; the purchases were already counted when
        # they posted, so counting the payment too reports money that was never
        # spent -- $1634.49 of it in the fixture, enough to outrank Groceries as
        # the top category in a table that claims to show spending.
        #
        # DELIBERATELY ASYMMETRIC WITH `pending`, which IS counted here. The
        # rule is whether the money is real, not whether the number is final: a
        # pending charge is committed money at a provisional amount, so it
        # belongs in a total (and is excluded only from find_recurring, where a
        # provisional amount would fabricate a price change). A transfer is a
        # final amount that was never spending at all. Opposite reasons, so
        # neither skip is evidence for the other.
        if txn.kind is TransactionKind.TRANSFER:
            continue
        if txn.amount_cents > 0:
            result[txn.category] = result.get(txn.category, 0) + txn.amount_cents
    return sorted(result.items(), key=lambda x: x[1], reverse=True)

def find_recurring(transactions):
    by_merchant = {}
    for txn in transactions:
        if txn.amount_cents <= 0:     # income or a zero entry, not a charge
            continue
        # A pending charge's amount is still provisional -- a card auth posts at
        # a different number all the time (tip, fuel hold, currency). Include it
        # and the detector invents a price change, which is the ONE thing this
        # feature exists to report. Wait for it to settle.
        if txn.pending:
            continue
        # A card payment is monthly, is one charge per month, and lands on gaps
        # of 31 and 32 days -- it satisfies every rule below, honestly, and is
        # not a subscription. Worse, you pay what you spent, so its varying
        # amount produces a "+$242.39" price-rise alert that means only that you
        # bought more last month. This reads `kind`, never the category string:
        # the fixture spells it "Transfer", Plaid "Transfer Out", a CSV
        # "Uncategorized".
        if txn.kind is TransactionKind.TRANSFER:
            continue
        by_merchant.setdefault((txn.account_id, txn.merchant), []).append(txn)

    found = []
    for (account_id, merchant), txns in by_merchant.items():
        # Year matters: over a 12+ month Plaid pull, a bare month number would
        # merge May 2026 with May 2027 and overstate the evidence.
        months = {(txn.date.year, txn.date.month) for txn in txns}
        if len(months) < MIN_MONTHS:
            continue

        # A subscription bills once a month. More charges than months is
        # ordinary shopping -- Starbucks has 15 charges across 3 months.
        if len(txns) != len(months):
            continue

        # Gaps only mean anything in date order, and Plaid hands back
        # transactions newest-first. Never assume the caller sorted them.
        txns = sorted(txns, key=lambda t: t.date)
        gaps = [(later.date - earlier.date).days
                for earlier, later in zip(txns, txns[1:])]
        if not all(MIN_GAP_DAYS <= gap <= MAX_GAP_DAYS for gap in gaps):
            continue

        # Weekend settlement only ever pushes a charge FORWARD, never back,
        # so the earliest day seen is the true billing anchor.
        days = [t.date.day for t in txns]
        amounts = [t.amount_cents for t in txns]

        # Not amounts[-2] -- that is merely the second-most-recent charge. If
        # the price rose and then held, [8431, 9177, 9177] would compare 9177
        # against 9177 and the rise would vanish. Reach back to the last
        # amount that actually differs.
        earlier = [cents for cents in amounts if cents != amounts[-1]]

        found.append(RecurringCharge(
            merchant=merchant,
            account_id=account_id,
            latest_cents=amounts[-1],
            previous_cents=earlier[-1] if earlier else None,
            day=min(days),
            last_date=txns[-1].date,
            occurrences=len(txns),
            evidence=tuple((t.date, t.amount_cents) for t in txns),
        ))

    return sorted(found, key=lambda c: (-c.latest_cents, c.merchant, c.account_id))


@dataclass(frozen=True)
class PriceIncrease:
    merchant: str
    account_id: str
    charge_id: str
    date: date
    old_cents: int
    new_cents: int

    @property
    def delta_cents(self) -> int:
        return self.new_cents - self.old_cents

    @property
    def key(self) -> tuple:
        return (self.account_id, self.merchant, self.charge_id)


RENEWAL_WINDOW_DAYS = 7


@dataclass(frozen=True)
class Renewal:
    merchant: str
    account_id: str
    expected: date
    cents: int
    latest_import: date | None
    overdue: bool


MAX_POSTING_DELAY_DAYS = 3


def is_month_end(day: date) -> bool:
    return day.day == monthrange(day.year, day.month)[1]


def billing_cycles(dates) -> list:
    cycles = []
    for posted in sorted(set(dates)):
        cycle = posted
        if cycles and posted.day <= MAX_POSTING_DELAY_DAYS and len(cycles) >= 2:
            try:
                before = posted - timedelta(days=posted.day)
            except OverflowError:
                cycles.append(cycle)
                continue
            previous = cycles[-1]
            one_month_on = ((before.year * 12 + before.month)
                            - (previous.year * 12 + previous.month)) == 1
            if one_month_on and all(is_month_end(c) for c in cycles):
                cycle = before
        cycles.append(cycle)
    return cycles


def billing_day(dates) -> int:
    days = []
    month_end_days = []
    for charged in dates:
        if is_month_end(charged):
            month_end_days.append(charged.day)
        else:
            days.append(charged.day)
    return min(days) if days else max(month_end_days)


def anchor_day(dates) -> int:
    return billing_day(billing_cycles(dates))


def next_billing_date(last_date: date, dates) -> date | None:
    year, month = last_date.year, last_date.month + 1
    if month == 13:
        year, month = year + 1, 1
    if year > date.max.year:
        return None
    day = min(billing_day(dates), monthrange(year, month)[1])
    return date(year, month, day)


def estimate_renewals(histories, latest_imports, today: date):
    try:
        last_day = today + timedelta(days=RENEWAL_WINDOW_DAYS)
    except OverflowError:
        last_day = date.max
    found = []
    for (account_id, merchant), charges in histories.items():
        if not charges:
            continue
        cycles = billing_cycles(d for d, _ in charges)
        expected = next_billing_date(cycles[-1], cycles)
        if expected is None or expected > last_day:
            continue
        found.append(Renewal(
            merchant=merchant,
            account_id=account_id,
            expected=expected,
            cents=charges[-1][1],
            latest_import=latest_imports.get(account_id),
            overdue=expected < today,
        ))
    return sorted(found, key=lambda r: (r.expected, r.merchant, r.account_id))


def latest_import_dates(transactions) -> dict:
    latest = {}
    for txn in transactions:
        if txn.account_id not in latest or txn.date > latest[txn.account_id]:
            latest[txn.account_id] = txn.date
    return latest
