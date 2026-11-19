"""
core/money.py
-------------
Money formatting, and (as of step 4.3) the one safe way to turn a provider's
amount into integer minor units.

Keeping both in one place is deliberate: there is exactly one function to review
and one function to test when the question is "did we lose a penny here".

`format_money` is the only place cents become text; `to_cents` is the only place
text becomes cents. Neither one rounds, and neither one touches float.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

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


#: What a well-formed amount string looks like: an optional sign, at least one
#: digit, and optionally a decimal point with at least one digit after it.
#: Decimal() alone is not a validator -- it happily accepts "NaN", "Infinity",
#: "1E+3" and surrounding whitespace, none of which is a bank amount. Anything
#: fancier than this pattern (a '$', a comma, "(12.34)" for negative) is a
#: *source format*, and unwrapping it is the CSV adapter's job at step 4.4;
#: doing it here would mean every future adapter silently inherits one CSV
#: dialect's conventions.
_AMOUNT_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")


def to_cents(amount: str | int | Decimal, currency: str = "USD") -> int:
    """Amount in MAJOR units -> integer minor units. Never rounds.

        "10.99", USD -> 1099
        "-45.00", USD -> -4500
        5, USD       -> 500        (a bare int is whole dollars, not cents)
        "1000", JPY  -> 1000       (JPY has no minor unit, so 1:1)
        "1.234", KWD -> 1234       (KWD has three)

    A BARE INT IS WHOLE UNITS. `to_cents(5)` is $5.00 -> 500, not 5 cents.
    That is the only reading that stays consistent with the string and Decimal
    cases (`to_cents("5")` must equal `to_cents(5)`) and with the currency
    scaling (`to_cents(1000, "JPY")` is 1000 yen either way). If you already
    hold minor units, you do not want this function at all -- pass the int
    straight to the model.

    float and bool are TypeErrors, not conversions:
      * float, because 10.99 is not 10.99 -- it is 10.9900000000000002131628...,
        and int(10.99 * 100) is 1098. There is no safe float amount, so there
        is no float path to get subtly wrong later.
      * bool, because `True` is an `int` to Python and would quietly become
        100. Same trap `_require_int_cents` guards in core/models.py.

    Anything that does not land exactly on a whole minor unit is a ValueError.
    Rounding here would be a silent penny loss attributed to no one; the caller
    that genuinely wants to round should say so at the call site.
    """
    digits = minor_units(currency)

    # bool first -- isinstance(True, int) is True, so the int branch below
    # would accept it.
    if isinstance(amount, bool):
        raise TypeError("to_cents() got a bool; that is not an amount")
    if isinstance(amount, float):
        raise TypeError(
            f"to_cents() got a float ({amount!r}); floats cannot represent "
            f"money exactly. Pass the string you received instead."
        )

    if isinstance(amount, str):
        text = amount
        if not _AMOUNT_RE.match(text):
            raise ValueError(
                f"malformed amount {amount!r}: expected digits with an "
                f"optional sign and at most one decimal point. Currency "
                f"symbols, thousands separators and parentheses are the "
                f"adapter's to strip, not this function's."
            )
        try:
            value = Decimal(text)
        except InvalidOperation:  # pragma: no cover - the regex got here first
            raise ValueError(f"malformed amount {amount!r}") from None
    elif isinstance(amount, (int, Decimal)):
        value = Decimal(amount)
    else:
        raise TypeError(
            f"to_cents() got {type(amount).__name__}; expected str, int or "
            f"Decimal"
        )

    if not value.is_finite():
        raise ValueError(f"malformed amount {amount!r}")
    sign, coefficient, exponent = value.as_tuple()
    end = len(coefficient)
    while end > 0 and coefficient[end - 1] == 0:
        end -= 1
    if end == 0:
        return 0
    exponent += len(coefficient) - end
    if exponent + digits < 0:
        raise ValueError(
            f"{amount!r} has more precision than {currency.upper()} allows "
            f"({digits} decimal place{'' if digits == 1 else 's'}); refusing "
            f"to round"
        )
    cents = int(Decimal((0, coefficient[:end], exponent + digits)))
    return -cents if sign else cents


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
