"""
tests/test_csv_source.py
------------------------
Unit tests for adapters/csv_source.py. Run from the project root:

    ./venv/Scripts/python.exe -m pytest tests/test_csv_source.py

These are NOT the baseline gate and do not replace it. The gate proves the
FAKE render has not moved; these prove the CSV adapter translates correctly,
and the two failures they exist to catch are both invisible in a render:

  * An inverted SIGN. Every number still formats perfectly, the tables still
    line up, the totals are still self-consistent -- and spending and income
    have swapped places. There is no pixel that looks wrong.
  * An unstable id. Nothing at all happens today; at Step 5 the second import
    of the same statement inserts a duplicate of every row instead of updating
    it.

The amount cleaner gets the first section of this file because it was written
and tested BEFORE anything called it -- see the module docstring in
adapters/csv_source.py.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from adapters.csv_source import (
    CURRENCY,
    SIGN,
    CsvAccount,
    CsvSource,
    _normalize_merchant,
    clean_amount,
)
from core.models import Provider

FIXTURE = str(Path(__file__).resolve().parent.parent / "fixtures" / "bank_export.csv")


# ---------------------------------------------------------------------------
# The amount cleaner (step 3) -- tested alone, first
# ---------------------------------------------------------------------------

def test_sign_constant_is_a_flip():
    """Pinned as a value, not just exercised through the cleaner.

    If someone "fixes" a display oddity by setting SIGN = 1, every assertion
    below that uses a negative input still needs updating -- but this one line
    is what says out loud that this bank writes debits negative and we write
    them positive.
    """
    assert SIGN == -1


@pytest.mark.parametrize("cell, expected", [
    # A DEBIT: the bank writes it negative, our model wants it positive,
    # because positive means money left the account.
    ("-$17.99", 1799),
    ("-17.99", 1799),
    ("-$1,204.77", 120477),
    # A CREDIT: the bank writes it positive, we store income negative.
    ("$2,300.00", -230000),
    ("2300.00", -230000),
    ("$0.05", -5),
    # Zero has no sign to flip.
    ("$0.00", 0),
    ("-$0.00", 0),
    # Whitespace, including the non-breaking space some exports emit.
    ("  -$45.00  ", 4500),
    ("-$45.00\t", 4500),
    ("-$1 234.00", 123400),
    # Multiple separators in one number.
    ("-$1,234,567.89", 123456789),
])
def test_clean_amount(cell, expected):
    assert clean_amount(cell) == expected


def test_the_comma_is_stripped_not_treated_as_a_decimal_point():
    """The failure this catches loses three orders of magnitude in silence.

    If the comma survived as a decimal separator, "$1,234.56" would parse as
    something near 1.23 and the row would render as a perfectly plausible
    small charge.
    """
    assert clean_amount("-$1,234.56") == 123456
    # The same number written without the separator must agree. If the comma
    # were read as a decimal point these two would differ by ~1000x.
    assert clean_amount("-$1,234.56") == clean_amount("-$1234.56")


def test_debit_and_credit_land_on_opposite_sides_of_zero():
    """The sign flip stated as the property it actually has, so a reader does
    not have to reconstruct the convention from the parametrized values."""
    spend = clean_amount("-$50.00")
    income = clean_amount("$50.00")
    assert spend > 0 > income
    assert spend == -income


def test_clean_amount_returns_a_real_int():
    """Not a Decimal, not a bool. core.models._require_int_cents would reject
    those, but only once a Transaction is constructed -- catch it here."""
    result = clean_amount("-$1.00")
    assert type(result) is int


@pytest.mark.parametrize("cell", [
    "(10.99)",        # parenthesised negative: a DIFFERENT dialect
    "10.99 CR",       # credit suffix: also a different dialect
    "1.234,56",       # comma decimal separator: would silently read as 123456
    "",
    "abc",
    "$",
    "-",
    "10.999",         # a third decimal in a two-decimal currency
])
def test_clean_amount_rejects_other_dialects_rather_than_guessing(cell):
    """Each of these has a plausible "helpful" reading, and every one of those
    readings is wrong for some other bank. Refusing forces the dialect to be
    added as an explicit branch that someone reviewed."""
    with pytest.raises(ValueError):
        clean_amount(cell)


def test_parse_error_names_the_original_cell():
    """The message must quote what was in the FILE, not the mangled leftovers
    after stripping, or the error cannot be matched to a row by eye."""
    with pytest.raises(ValueError, match=r"\(10\.99\)"):
        clean_amount("(10.99)")


# ---------------------------------------------------------------------------
# The merchant normalizer (step 4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("description, expected", [
    # Prefix table: the tail is store numbers, phone numbers and cities.
    ("HULU 877-8244858 CA", "Hulu"),
    ("SAFEWAY #1842 SEATTLE WA", "Safeway"),
    ("SHELL OIL 57442310", "Shell"),
    ("COSTCO WHSE #0117", "Costco"),
    ("DOORDASH*TACO TIME", "DoorDash"),
    ("HOME DEPOT #4721", "Home Depot"),
    ("ACME CORP PAYROLL", "Payroll Deposit"),
    # Table values are used VERBATIM -- .title() would make this "Rei".
    ("REI #011 SEATTLE", "REI"),
    # Fallback: first two words, title-cased.
    ("PANERA BREAD #601234", "Panera Bread"),
    ("TARGET", "Target"),
    ("  KEXP  MEMBERSHIP  SEATTLE ", "Kexp Membership"),
])
def test_normalize_merchant(description, expected):
    assert _normalize_merchant(description) == expected


def test_the_normalizer_groups_where_a_raw_descriptor_would_not():
    """The entire point of normalizing. Three descriptors, one merchant --
    without this, "Recurring Charges" sees three merchants with one charge
    each and reports nothing."""
    descriptors = [
        "SAFEWAY #1842 SEATTLE WA",
        "SAFEWAY #0221 BELLEVUE WA",
        "SAFEWAY FUEL #1842",
    ]
    assert len({_normalize_merchant(d) for d in descriptors}) == 1


def test_merchant_is_never_empty():
    """A model invariant, and one Transaction does not itself enforce. An
    empty merchant makes a group keyed on "" that swallows every such row."""
    # "descriptor", not "Description": the normalizer moved to
    # adapters/merchant.py at 4.7 and stopped naming a CSV column, because a
    # Plaid `name` arrives at the same function.
    with pytest.raises(ValueError, match="blank descriptor"):
        _normalize_merchant("   ")


# ---------------------------------------------------------------------------
# Parsing the fixture end to end
# ---------------------------------------------------------------------------

@pytest.fixture
def source():
    return CsvSource(path=FIXTURE)


def test_dates_parse_as_month_day_year(source):
    """05/08/2026 is May 8th, not August 5th. The two readings are
    indistinguishable for the first twelve days of every month, so this is
    pinned on a row where they differ."""
    txns = source.fetch_transactions()

    hulus = sorted(t.date for t in txns if t.merchant == "Hulu")
    assert hulus == [date(2026, 5, 8), date(2026, 6, 8), date(2026, 7, 8)]

    # 07/13 is the row that would be unparseable under "%d/%m/%Y" -- there is
    # no 7th day-of-week-13 month, so a bank shipping DD/MM would crash here
    # rather than silently transposing. Most rows are not that lucky.
    assert date(2026, 7, 13) in {t.date for t in txns}

    # The RAW descriptor survives verbatim onto `name`; only `merchant` is
    # normalized. Losing the raw text would make every minted id unauditable.
    assert "HULU 877-8244858 CA" in {t.name for t in txns}


def test_amounts_are_ints_in_our_sign_convention(source):
    txns = source.fetch_transactions()
    assert all(type(t.amount_cents) is int for t in txns)

    hulu = [t for t in txns if t.merchant == "Hulu"]
    assert [t.amount_cents for t in hulu] == [1799, 1799, 1999]

    # The payroll row is the only credit in the file, and it must be the only
    # negative amount. If SIGN were inverted this assertion flips wholesale.
    negatives = [t for t in txns if t.amount_cents < 0]
    assert [t.merchant for t in negatives] == ["Payroll Deposit"]


def test_thousands_separator_row_survives_the_round_trip(source):
    """$1,204.77 in the file must be 120477 cents, not 120 or 1204."""
    costco = [t for t in source.fetch_transactions()
              if t.merchant == "Costco" and t.date == date(2026, 5, 28)]
    assert [t.amount_cents for t in costco] == [120477]


def test_currency_and_pending_are_set(source):
    txns = source.fetch_transactions()
    assert all(t.currency == CURRENCY for t in txns)
    assert not any(t.pending for t in txns)


def test_every_transaction_resolves_to_the_account(source):
    """The structural invariant the dashboard is blind to: it never joins a
    transaction to its account, so a bare account_id renders perfectly."""
    account_ids = {a.id for a in source.fetch_accounts()}
    assert account_ids
    assert {t.account_id for t in source.fetch_transactions()} <= account_ids


def test_transactions_come_back_in_date_order(source):
    dates = [t.date for t in source.fetch_transactions()]
    assert dates == sorted(dates)


# ---------------------------------------------------------------------------
# Ids
# ---------------------------------------------------------------------------

def test_ids_are_stable_across_two_reads(source):
    """The property that turns Step 5's write into an upsert. Two separate
    fetches of the same unchanged file must produce the same ids, in the same
    order -- including for the duplicate-content rows, whose occurrence
    counter lives on a fresh minter each time."""
    first = [t.id for t in source.fetch_transactions()]
    second = [t.id for t in CsvSource(path=FIXTURE).fetch_transactions()]
    assert first == second
    assert len(set(first)) == len(first), "ids must be unique within one fetch"


def test_ids_do_not_depend_on_row_order(tmp_path):
    """Re-export the same statement with the rows shuffled and every id must
    match. This is what position-based numbering (txn_001..) gets wrong: the
    same real-world charge would come back with a different key."""
    lines = Path(FIXTURE).read_text(encoding="utf-8-sig").splitlines()
    header, rows = lines[0], lines[1:]

    shuffled = tmp_path / "shuffled.csv"
    shuffled.write_text("\n".join([header, *reversed(rows)]) + "\n",
                        encoding="utf-8")

    original = {t.id for t in CsvSource(path=FIXTURE).fetch_transactions()}
    reordered = {t.id for t in CsvSource(path=str(shuffled)).fetch_transactions()}
    assert original == reordered


def test_inserting_a_row_rekeys_only_that_row(tmp_path):
    """The other half of content addressing. Position-based ids would shift
    every id below the insertion point, and at Step 5 that rewrites rows
    rather than adding one."""
    text = Path(FIXTURE).read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    injected = tmp_path / "injected.csv"
    injected.write_text(
        "\n".join([lines[0], "05/15/2026,QFC #874 SEATTLE WA,-$61.20", *lines[1:]]) + "\n",
        encoding="utf-8",
    )

    before = {t.id for t in CsvSource(path=FIXTURE).fetch_transactions()}
    after = {t.id for t in CsvSource(path=str(injected)).fetch_transactions()}
    assert before < after
    assert len(after - before) == 1


def test_csv_ids_cannot_collide_with_fixture_ids():
    """Namespacing, checked against the other adapter rather than asserted
    about a string shape. Even if a CSV row hashed to exactly the same digest
    as a fixture row, "csv:..." and "fake:..." are different keys."""
    from adapters.fake import FakeSource

    csv_ids = {t.id for t in CsvSource(path=FIXTURE).fetch_transactions()}
    csv_ids |= {a.id for a in CsvSource(path=FIXTURE).fetch_accounts()}

    fake = FakeSource()
    fake_ids = {t.id for t in fake.fetch_transactions()}
    fake_ids |= {a.id for a in fake.fetch_accounts()}

    assert not (csv_ids & fake_ids)
    assert all(i.startswith("csv:") for i in csv_ids)


def test_ids_carry_typed_provenance(source):
    """Provenance is READ off SourceRef, never parsed back out of the id."""
    txn = source.fetch_transactions()[0]
    assert txn.source.provider is Provider.CSV
    assert txn.source.connection_id == "bank_export"


# ---------------------------------------------------------------------------
# The account, which the file does not contain
# ---------------------------------------------------------------------------

def test_account_identity_comes_from_the_constructor():
    account = CsvAccount(
        provider_id="chk_9021",
        name="Everyday Checking",
        institution="Wells Fargo",
    )
    (built,) = CsvSource(path=FIXTURE, account=account).fetch_accounts()

    assert built.name == "Everyday Checking"
    assert built.institution == "Wells Fargo"
    assert built.id == "csv:bank_export:chk_9021"
    # A statement is not a balance. Summing the rows would give the period's
    # net change while looking exactly like one.
    assert built.balance_cents == 0


def test_path_falls_back_to_the_env_var_then_the_default(monkeypatch):
    monkeypatch.setenv("CSV_PATH", "somewhere/else.csv")
    assert CsvSource().path == "somewhere/else.csv"
    # An explicit path always wins over the environment.
    assert CsvSource(path=FIXTURE).path == FIXTURE

    monkeypatch.delenv("CSV_PATH")
    assert CsvSource().path == "fixtures/bank_export.csv"


# ---------------------------------------------------------------------------
# What the dashboard is supposed to find in this file
# ---------------------------------------------------------------------------

def test_the_subscription_is_detected_with_its_price_rise(source):
    """The end-to-end reason this adapter exists: insights.py, unchanged, must
    find the subscription in a CSV it knows nothing about."""
    from insights import find_recurring

    charges = {c.merchant: c for c in find_recurring(source.fetch_transactions())}
    assert "Hulu" in charges

    hulu = charges["Hulu"]
    assert hulu.latest_cents == 1999
    assert hulu.previous_cents == 1799
    assert hulu.delta_cents == 200
    assert hulu.direction == "up"
    assert hulu.occurrences == 3


def test_the_noise_is_not_flagged_as_recurring(source):
    """Shell appears in all three months and is NOT a subscription -- its gaps
    are 37 and 33 days. Safeway appears in all three months too, but twice in
    July. Both are the shapes a naive "seen every month" rule would flag."""
    from insights import find_recurring

    merchants = {c.merchant for c in find_recurring(source.fetch_transactions())}
    assert merchants == {"Hulu"}
