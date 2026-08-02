"""
adapters/merchant.py
--------------------
Raw descriptor -> a merchant name that is never empty. SHARED, because more
than one adapter now needs it and neither should own it.

It started inside csv_source.py, where a CSV descriptor was the only thing that
ever needed cleaning. Plaid changed that: `merchant_name` is null on roughly
half of real rows, and the fallback is the raw `name` -- the same problem the
CSV adapter already solved. Importing it from csv_source would have worked and
been wrong: adapters must not depend on each other, and the error message
("blank Description") named a CSV COLUMN, which is meaningless to a caller
holding a Plaid payload.

WHY THIS IS NOT IN core/
It is translation, not vocabulary. `core/models.py` states that merchant is
never empty; deciding that "AMZN Mktp US*2X4B1QR03" means "Amazon" is a guess
about one provider's formatting conventions, and guesses belong on the adapter
side of the seam.

NOT AN ENTITY RESOLVER, and deliberately so. Real merchant cleaning (fuzzy
matching, stripping store numbers, cities, phone numbers) is a rabbit hole that
produces confidently wrong names and, worse, ones that CHANGE -- and merchant
is a grouping key. Improving it must never move a historical id, which is why
`fingerprint.mint()` is fed the raw descriptor and never this function's output.
"""

from __future__ import annotations

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


def normalize_merchant(description: str) -> str:
    """Raw descriptor -> a merchant name that is never empty.

        "HULU 877-8244858 CA"      -> "Hulu"          (prefix table)
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
    text = description.strip() if description else ""
    if not text:
        # An empty merchant violates a model invariant, and Transaction would
        # not catch it (only currency and amount are guarded). Fail at the
        # record that caused it rather than three layers up in a group keyed
        # on "". Phrased without naming any one source's field, because both
        # a CSV "Description" and a Plaid "name" arrive here.
        raise ValueError(
            "blank descriptor: cannot derive a merchant, and the model "
            "forbids an empty one because grouping happens on it"
        )

    upper = text.upper()
    # Longest first: "SHELL OIL" must win over a hypothetical "SHELL".
    for prefix in sorted(MERCHANT_PREFIXES, key=len, reverse=True):
        if upper.startswith(prefix.upper()):
            return MERCHANT_PREFIXES[prefix]

    return " ".join(text.split()[:2]).title()
