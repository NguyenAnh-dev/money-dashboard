"""
tests/test_money.py
-------------------
Unit tests for core.money -- `to_cents`, and its round trip through
`format_money`. Run from the project root:

    ./venv/Scripts/python.exe -m pytest

These are NOT part of the baseline gate and cannot be: `to_cents` has no caller
yet (the fixture in adapters/fake.py already hands over ints), so it moves no
pixel of the render. That is exactly why it needs tests of its own -- the
conversion that turns "10.99" into 1099 is the one place a penny can disappear,
and it would disappear silently.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.money import format_money, to_cents


# ---------------------------------------------------------------------------
# to_cents -- accepted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("amount, currency, expected", [
    ("10.99", "USD", 1099),
    ("0", "USD", 0),
    ("-45.00", "USD", -4500),
    ("1000", "JPY", 1000),     # no minor unit: 1000 yen is 1000, not 100000
    ("1.234", "KWD", 1234),    # three minor digits, so .234 is exact
    ("+7.50", "USD", 750),
    ("-0.01", "USD", -1),
    (Decimal("10.99"), "USD", 1099),
])
def test_to_cents_converts(amount, currency, expected):
    assert to_cents(amount, currency) == expected


def test_bare_int_is_whole_units():
    """A bare int is MAJOR units, and must agree with its string spelling.

    `to_cents(5)` is $5.00 -> 500, not 5 cents. If this ever flips, every
    caller that passes an int is off by 100x in silence.
    """
    assert to_cents(5, "USD") == 500
    assert to_cents(5, "USD") == to_cents("5", "USD")
    assert to_cents(1000, "JPY") == 1000


def test_returns_a_real_int_not_a_bool_or_decimal():
    result = to_cents("1.00", "USD")
    assert type(result) is int


# ---------------------------------------------------------------------------
# to_cents -- rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("amount", [
    10.99,   # float: int(10.99 * 100) == 1098, a penny gone in silence
    0.0,
    True,    # bool is an int to Python and would quietly become 100
    None,
    [],
])
def test_to_cents_rejects_wrong_types(amount):
    with pytest.raises(TypeError):
        to_cents(amount, "USD")


@pytest.mark.parametrize("amount, currency", [
    ("10.999", "USD"),   # third decimal in a two-digit currency
    ("10.5", "JPY"),     # there is no fractional yen
    ("1.2345", "KWD"),   # one digit past KWD's three
    ("abc", "USD"),
    ("", "USD"),
    ("-", "USD"),
    (".", "USD"),
    # Everything below is a SOURCE FORMAT. Unwrapping it is the CSV adapter's
    # job at step 4.4; doing it here would make every future adapter inherit
    # one CSV dialect's conventions.
    ("$10.99", "USD"),
    ("1,000.00", "USD"),
    ("(10.99)", "USD"),
    # These two are the reason a regex guards Decimal() instead of a bare
    # try/except: Decimal accepts both, and neither is a bank amount.
    (" 10.99 ", "USD"),
    ("1e3", "USD"),
    ("NaN", "USD"),
    ("Infinity", "USD"),
])
def test_to_cents_rejects_malformed_amounts(amount, currency):
    with pytest.raises(ValueError):
        to_cents(amount, currency)


def test_refuses_to_round_rather_than_losing_a_fraction():
    """The failure has to be loud. A silent round is a penny attributed to
    nobody, discovered a month later as a balance that does not tie out."""
    with pytest.raises(ValueError, match="refusing to round"):
        to_cents("10.999", "USD")


# ---------------------------------------------------------------------------
# Round trip -- the two halves of money.py pinned against each other
# ---------------------------------------------------------------------------
# Neither function's own tests can catch a disagreement between them: to_cents
# could scale by 10 and format_money could divide by 10 and both would look
# self-consistent. These go text -> cents -> text and demand the original back.

@pytest.mark.parametrize("text, currency", [
    ("10.99", "USD"),
    ("0.00", "USD"),
    ("-45.00", "USD"),
    ("872.15", "USD"),
    ("1000", "JPY"),     # 0 minor units: format_money must emit no decimal point
    ("-250", "JPY"),
    ("1.234", "KWD"),    # 3 minor units: the digit that a hardcoded 2 would eat
    ("-0.500", "KWD"),
])
def test_round_trip_text_to_cents_to_text(text, currency):
    from core.money import SYMBOLS

    cents = to_cents(text, currency)
    rendered = format_money(cents, currency)

    # Rebuild what format_money should have produced, from the input string.
    # Sign goes OUTSIDE the symbol ('-$872.15'), which is how a statement reads.
    sign, body = ("-", text[1:]) if text.startswith("-") else ("", text)
    symbol = SYMBOLS.get(currency.upper())
    expected = f"{sign}{symbol}{body}" if symbol else f"{sign}{body} {currency}"

    assert rendered == expected


def test_round_trip_spot_checks():
    """The same property, spelled out literally, so a reader can see the shape
    without running the parametrized reconstruction above in their head."""
    assert format_money(to_cents("10.99", "USD"), "USD") == "$10.99"
    assert format_money(to_cents("1000", "JPY"), "JPY") == "¥1000"
    assert format_money(to_cents("1.234", "KWD"), "KWD") == "1.234 KWD"


@pytest.mark.parametrize("cents, currency", [
    (1099, "USD"), (0, "USD"), (-4500, "USD"),
    (1000, "JPY"), (-7, "JPY"),
    (1234, "KWD"), (-500, "KWD"),
])
def test_round_trip_cents_first(cents, currency):
    """The other direction: cents -> text -> cents. This is the one that a CSV
    export/import cycle will actually perform, once step 4.4 exists."""
    rendered = format_money(cents, currency)

    # Strip only what format_money added -- the symbol or trailing code -- and
    # put the sign back on the front. to_cents does no stripping by design.
    body = rendered.lstrip("-")
    if body.endswith(f" {currency}"):
        body = body[: -len(currency) - 1]
    else:
        body = body.lstrip("$€£¥")
    if rendered.startswith("-"):
        body = "-" + body

    assert to_cents(body, currency) == cents


def test_to_cents_is_exact_beyond_the_integer_string_limit():
    digits = 4301
    whole = (10 ** digits - 1) // 9
    assert whole > 10 ** 4300

    as_string = to_cents("1" * digits)
    as_decimal = to_cents(Decimal("1" * digits))
    as_int = to_cents(whole)

    assert as_string == as_decimal == as_int == whole * 100
    assert as_string != 0
    assert to_cents("-" + "1" * digits) == -whole * 100
    assert to_cents(whole, "JPY") == whole
    assert to_cents(whole, "KWD") == whole * 1000
