from dataclasses import dataclass
from datetime import date

MIN_MONTHS = 3
MIN_GAP_DAYS = 26
MAX_GAP_DAYS = 35


@dataclass
class RecurringCharge:
    merchant: str
    latest_cents: int           # what it costs now
    previous_cents: int | None  # last amount that DIFFERS from latest;
                                # None means the price has never moved
    day: int                    # billing anchor: earliest day-of-month seen
    last_date: date
    occurrences: int

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
        if txn.amount_cents > 0:
            result[txn.category] = result.get(txn.category, 0) + txn.amount_cents
    return sorted(result.items(), key=lambda x: x[1], reverse=True)

def find_recurring(transactions):
    by_merchant = {}
    for txn in transactions:
        if txn.amount_cents < 0:      # income, not a charge
            continue
        by_merchant.setdefault(txn.merchant, []).append(txn)

    found = []
    for merchant, txns in by_merchant.items():
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
            latest_cents=amounts[-1],
            previous_cents=earlier[-1] if earlier else None,
            day=min(days),
            last_date=txns[-1].date,
            occurrences=len(txns),
        ))

    return sorted(found, key=lambda c: c.latest_cents, reverse=True)