"""
core/money.py
-------------
Money formatting, and (from step 4.3) the one safe way to turn a provider's
amount into integer cents.

Keeping both in one place is deliberate: there is exactly one function to review
and one function to test when the question is "did we lose a penny here".
"""

#: Digits after the decimal point, per ISO-4217. Most currencies use 2, but
#: assuming 2 is a real bug: a JPY amount divided by 100 is off by 100x, and
#: JOD/KWD/BHD use 3. Only the ones likely to show up are listed; the default
#: of 2 covers USD, EUR, GBP and friends.
MINOR_UNITS: dict[str, int] = {
    "JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "ISK": 0,
    "JOD": 3, "KWD": 3, "BHD": 3, "TND": 3, "OMR": 3,
}

#: Display symbols. Anything absent renders as "12.34 CHF" instead of guessing.
SYMBOLS: dict[str, str] = {
    "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥",
}


def minor_units(currency: str) -> int:
    """How many digits after the decimal point this currency has."""
    return MINOR_UNITS.get(currency.upper(), 2)


def format_money(cents: int, currency: str = "USD") -> str:
    """Integer minor units -> display string.

        1234, USD   -> '$12.34'
        -87215, USD -> '-$872.15'
        1234, JPY   -> '¥1234'      (no decimal point; JPY has no minor unit)
        1234, CHF   -> '12.34 CHF'  (no symbol known, so the code is shown)

    The sign goes OUTSIDE the symbol ('-$872.15'), which is how a statement
    reads. Note there is no thousands separator -- see the docstring note below.
    """
    currency = currency.upper()
    digits = minor_units(currency)

    sign = "-" if cents < 0 else ""
    cents = abs(cents)

    if digits == 0:
        body = str(cents)
    else:
        whole, frac = divmod(cents, 10 ** digits)
        body = f"{whole}.{frac:0{digits}d}"

    symbol = SYMBOLS.get(currency)
    if symbol:
        return f"{sign}{symbol}{body}"
    return f"{sign}{body} {currency}"


# NOTE on thousands separators: `dollars()` never had them, so neither does
# this -- adding one here would change every rendered row and break the step
# 4.2 baseline gate for a cosmetic reason. It is a display change, worth making
# on its own later, with the diff reviewed on purpose.
