"""
tests/test_plaid_source.py
--------------------------
The Plaid adapter's TRANSLATION, verified against a real recorded sandbox
payload (`fixtures/plaid_sandbox.json`) rather than against the documented
schema.

No credentials, no network, no SDK import. That is deliberate and it is the
reason `plaid_source.py` splits translation from I/O: every invariant worth
checking is a property of the mapping, so binding those checks to a live API
call would mean they run only on the one machine that has a token exported.

The fixture is a SNAPSHOT. Plaid's sandbox regenerates data per Item with dates
relative to the recording date, so nothing here asserts on absolute dates or on
exact counts beyond "there is some data".
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "plaid_sandbox.json"

from adapters.plaid_source import (  # noqa: E402
    ACCOUNT_TYPES,
    UNCATEGORIZED,
    AccountType,
    _balance_to_cents,
    _to_cents,
    _to_date,
    account_type_of,
    category_of,
    merchant_of,
    to_account,
    to_transaction,
)
from core.models import DEBT_TYPES, Provider, make_id  # noqa: E402

#: Any non-empty string works: the fixture does not record an item_id (it is
#: not part of either payload), and every id assertion below rebuilds the id
#: from the record's own SourceRef rather than from this constant.
ITEM_ID = "recorded_sandbox_item"


@pytest.fixture(scope="module")
def payload() -> dict:
    if not FIXTURE.exists():
        pytest.skip(
            f"{FIXTURE.name} not recorded. Run "
            f"scripts/get_plaid_token.py --record-only"
        )
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def accounts(payload) -> list:
    return [to_account(raw, ITEM_ID) for raw in payload["accounts"]]


@pytest.fixture(scope="module")
def transactions(payload) -> list:
    return [to_transaction(raw, ITEM_ID) for raw in payload["transactions"]]


# ---------------------------------------------------------------------------
# The fixture itself -- if these fail, the recording is the problem
# ---------------------------------------------------------------------------

def test_the_fixture_actually_has_transactions(payload):
    """The recording bug this test exists because of.

    The first recorded fixture held 12 accounts and ZERO transactions: sandbox
    answers an early sync with a successful, empty page rather than an error,
    so the script's retry never fired and it wrote a file that made the
    transaction mapping look tested while exercising none of it.
    """
    assert payload["accounts"], "fixture has no accounts"
    assert payload["transactions"], (
        "fixture has no transactions -- re-record with --record-only. Every "
        "transaction assertion below would vacuously pass."
    )


def test_the_fixture_carries_no_credentials(payload):
    """It is committed, so this is checked rather than assumed."""
    blob = json.dumps(payload)
    for probe in ("access-sandbox", "access_token", "secret", "client_id"):
        assert probe not in blob, f"{probe!r} leaked into the committed fixture"


# ---------------------------------------------------------------------------
# The traps, each asserted against the real payload
# ---------------------------------------------------------------------------

def test_every_amount_arrives_as_a_float(payload):
    """The premise of `_to_cents`, pinned so it is not merely believed.

    If Plaid ever starts sending strings this test fails, which is the signal
    to revisit the conversion -- not a silent change in behaviour.
    """
    amounts = [t["amount"] for t in payload["transactions"]]
    assert amounts and all(isinstance(a, float) for a in amounts)


@pytest.mark.parametrize("amount, naive, correct", [
    (1.15, 114, 115),
    (4.35, 434, 435),
])
def test_int_times_100_loses_a_cent_and_the_str_detour_does_not(amount, naive,
                                                                correct):
    """The bug `_to_cents` exists to avoid, pinned in both directions.

    WORTH KNOWING: none of the 48 recorded transactions trigger it. An adapter
    written as `int(amount * 100)` passes every fixture-driven assertion in
    this file and still loses a cent in production the first time a $1.15
    charge arrives. That is exactly why this case is a unit test with chosen
    inputs rather than something the recording was expected to catch.
    """
    assert int(amount * 100) == naive        # the wrong way
    assert _to_cents(amount, "USD") == correct   # the right way


def test_amounts_survive_the_float_conversion_exactly():
    assert _to_cents(6.33, "USD") == 633
    assert _to_cents(110.0, "USD") == 11000
    assert _to_cents(-45.5, "USD") == -4550
    assert _to_cents(5, "USD") == 500        # a bare int is whole units


def test_the_str_detour_launders_parsing_noise_but_not_arithmetic_noise():
    """The exact limit of what `str(float)` buys, worth stating precisely.

    `str()` returns the shortest decimal that round-trips, so a float PARSED
    from "6.33" becomes "6.33" again -- which is every amount Plaid sends,
    because they arrive as decimal literals in JSON.

    It does NOT rescue a float produced by arithmetic: 0.1 + 0.2 really is
    0.30000000000000004, and that is its shortest round-tripping form. The
    conversion refuses it rather than rounding, which is the right outcome --
    an amount nobody can write down exactly should stop the import, not be
    quietly smoothed into 30 cents.
    """
    assert str(0.1 + 0.2) == "0.30000000000000004"
    with pytest.raises(ValueError, match="more precision"):
        _to_cents(0.1 + 0.2, "USD")


def test_a_sub_cent_transaction_amount_is_refused_not_rounded():
    """Transactions are strict. A fraction of a cent on something somebody was
    charged is a bug in the payload or in our reading of it, not noise."""
    with pytest.raises(ValueError, match="more precision"):
        _to_cents(12.3456, "USD")


def test_a_sub_cent_BALANCE_is_rounded_because_a_balance_is_a_snapshot():
    """Found in the real payload: the 401k reports current = 23631.9805.

    Strict conversion would fail the whole sync on an ordinary retirement
    account, so balances round and transactions do not.
    """
    assert _balance_to_cents(23631.9805, "USD") == 2363198
    assert _balance_to_cents(110.0, "USD") == 11000


def test_the_recorded_payload_still_contains_the_sub_cent_balance(payload):
    """If a re-recording drops it, the rounding path above stops being
    exercised by real data and this says so."""
    from decimal import Decimal as D
    tails = [
        -D(str(a["balances"]["current"])).as_tuple().exponent
        for a in payload["accounts"] if a["balances"].get("current") is not None
    ]
    assert any(t > 2 for t in tails), (
        "no sub-cent balance in this recording; _balance_to_cents is now only "
        "covered by the synthetic test above"
    )


def test_to_cents_rejects_a_bool():
    with pytest.raises(TypeError):
        _to_cents(True, "USD")


def test_merchant_name_is_null_on_a_large_share_of_rows(payload):
    """The fallback is the COMMON path, not an edge case.

    Asserted as a share rather than a count because the fixture is a snapshot
    and re-recording changes the exact number.
    """
    total = len(payload["transactions"])
    null = sum(1 for t in payload["transactions"] if t.get("merchant_name") is None)
    assert null > 0, (
        "no null merchant_name in this recording -- the fallback path is "
        "untested, which is the one thing this fixture was recorded for"
    )
    assert null / total > 0.1


def test_merchant_is_never_empty_even_where_plaid_supplies_nothing(transactions):
    """Invariant 4, on the half of rows Plaid does not name."""
    for txn in transactions:
        assert txn.merchant and txn.merchant.strip(), (
            f"{txn.id}: merchant {txn.merchant!r} from name {txn.name!r}"
        )


def test_merchant_falls_back_to_the_normalizer():
    assert merchant_of({"merchant_name": "Uber", "name": "Uber 072515"}) == "Uber"
    assert merchant_of({"merchant_name": None,
                        "name": "PANERA BREAD #601234"}) == "Panera Bread"
    # Whitespace-only is treated as absent and falls through to the raw name.
    # Note the fallback keeps TWO words by design, so the store number
    # survives here -- "Target 00021", not "Target". That is the normalizer's
    # documented behaviour (predictable beats clever), not a defect.
    assert merchant_of({"merchant_name": "   ",
                        "name": "TARGET 00021"}) == "Target 00021"
    assert merchant_of({"merchant_name": None, "name": "TARGET"}) == "Target"


# ---------------------------------------------------------------------------
# Accounts: the type table and the debt flip
# ---------------------------------------------------------------------------

def test_the_recorded_payload_exercises_every_account_family(payload):
    """12 sandbox accounts spanning depository/credit/loan/investment.

    Stated as a fixture property because it is what makes the table below
    meaningfully tested rather than tested on checking accounts alone.
    """
    families = {a["type"] for a in payload["accounts"]}
    assert {"depository", "credit", "loan", "investment"} <= families


def test_account_types_map_to_ours(accounts):
    kinds = {a.type for a in accounts}
    assert AccountType.CHECKING in kinds
    assert AccountType.SAVINGS in kinds
    assert AccountType.CREDIT in kinds
    assert AccountType.LOAN in kinds
    assert AccountType.INVESTMENT in kinds


@pytest.mark.parametrize("raw, expected", [
    ({"type": "depository", "subtype": "checking"}, AccountType.CHECKING),
    ({"type": "depository", "subtype": "money market"}, AccountType.SAVINGS),
    ({"type": "credit", "subtype": "credit card"}, AccountType.CREDIT),
    ({"type": "loan", "subtype": "mortgage"}, AccountType.LOAN),
    ({"type": "investment", "subtype": "401k"}, AccountType.INVESTMENT),
    # Unknown SUBTYPE falls back to the family, not to OTHER.
    ({"type": "loan", "subtype": "yacht"}, AccountType.LOAN),
    # Unknown TYPE is OTHER, and does not raise.
    ({"type": "cryptocurrency", "subtype": "wallet"}, AccountType.OTHER),
    ({"type": None, "subtype": None}, AccountType.OTHER),
    ({}, AccountType.OTHER),
])
def test_account_type_of_never_raises(raw, expected):
    assert account_type_of(raw) == expected


def test_debt_accounts_come_out_negative(accounts):
    """The flip, on real data. Plaid reports card balances as positive debt.

    Getting this backwards moves net worth by twice the balance while every
    individual row still renders plausibly -- which is why the conformance
    suite armed this check at 4.2, before there was an adapter to catch.
    """
    debts = [a for a in accounts if a.type in DEBT_TYPES]
    assert debts, "fixture has no credit or loan account to check the flip on"
    for account in debts:
        assert account.balance_cents <= 0, (
            f"{account.name}: debt account holds {account.balance_cents}; "
            f"net worth would be off by {2 * account.balance_cents}"
        )


def test_the_flip_is_applied_to_debt_and_only_to_debt():
    """Both directions, because only asserting the negative half would pass on
    an adapter that negated everything."""
    card = to_account({
        "account_id": "a1", "type": "credit", "subtype": "credit card",
        "name": "Card", "balances": {"current": 410.0, "iso_currency_code": "USD"},
    }, ITEM_ID)
    assert card.balance_cents == -41000

    checking = to_account({
        "account_id": "a2", "type": "depository", "subtype": "checking",
        "name": "Checking",
        "balances": {"current": 110.0, "iso_currency_code": "USD"},
    }, ITEM_ID)
    assert checking.balance_cents == 11000


def test_a_missing_balance_becomes_zero_not_a_guess():
    account = to_account({
        "account_id": "a3", "type": "investment", "subtype": "401k",
        "name": "401k",
        "balances": {"current": None, "available": 5.0,
                     "iso_currency_code": "USD"},
    }, ITEM_ID)
    assert account.balance_cents == 0


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def test_amounts_are_integer_minor_units(accounts, transactions):
    for value in ([a.balance_cents for a in accounts]
                  + [t.amount_cents for t in transactions]):
        assert type(value) is int


def test_no_sign_flip_is_applied(payload, transactions):
    """Plaid already matches our convention, so the mapping is identity.

    Written as a comparison against the raw payload rather than as a spot
    check, because an inverted flip has no visible symptom: every number still
    formats and spending and income simply swap places.
    """
    by_id = {t["transaction_id"]: t for t in payload["transactions"]}
    for txn in transactions:
        raw_amount = by_id[txn.source.provider_id]["amount"]
        assert (txn.amount_cents > 0) == (raw_amount > 0), (
            f"{txn.merchant}: raw {raw_amount} became {txn.amount_cents}"
        )
        assert txn.amount_cents == _to_cents(raw_amount, txn.currency)


def test_the_payload_contains_both_signs(payload):
    """Otherwise the test above is vacuous in one direction."""
    amounts = [t["amount"] for t in payload["transactions"]]
    assert any(a > 0 for a in amounts) and any(a < 0 for a in amounts)


def test_dates_are_date_and_not_datetime(transactions):
    """`type(x) is date`, NOT isinstance.

    datetime is a subclass of date, so isinstance(datetime.now(), date) is
    True -- an isinstance check cannot see a datetime slipping through, and the
    time component perturbs every day-gap in find_recurring.
    """
    for txn in transactions:
        assert type(txn.date) is date, (
            f"{txn.id}: date is {type(txn.date).__name__}"
        )


def test_to_date_reads_all_three_shapes():
    """The fixture stores ISO strings; the live SDK returns date objects. Both
    must land on the same value or the tests exercise a path production does
    not run."""
    assert _to_date("2026-07-26") == date(2026, 7, 26)
    assert _to_date(date(2026, 7, 26)) == date(2026, 7, 26)

    narrowed = _to_date(datetime(2026, 7, 26, 13, 45))
    assert narrowed == date(2026, 7, 26)
    assert type(narrowed) is date


def test_pending_is_a_bool(transactions):
    for txn in transactions:
        assert isinstance(txn.pending, bool)


def test_categories_are_formatted_not_raw_enum_names(transactions):
    for txn in transactions:
        assert txn.category
        assert "_" not in txn.category, f"{txn.category!r} is still an enum name"


@pytest.mark.parametrize("raw, expected", [
    ({"personal_finance_category": {"primary": "TRANSPORTATION"}}, "Transportation"),
    ({"personal_finance_category": {"primary": "FOOD_AND_DRINK"}}, "Food And Drink"),
    ({"personal_finance_category": {}}, UNCATEGORIZED),
    ({"personal_finance_category": None}, UNCATEGORIZED),
    ({}, UNCATEGORIZED),
])
def test_category_of(raw, expected):
    assert category_of(raw) == expected


# ---------------------------------------------------------------------------
# Ids
# ---------------------------------------------------------------------------

def test_ids_are_plaids_own_not_a_content_hash(payload, transactions):
    """`transaction_id` verbatim as the provider_id -- no fingerprinting.

    Plaid supplies stable ids, so the content-hash scheme (and its
    duplicate-row ceiling) never applies here.
    """
    raw_ids = {t["transaction_id"] for t in payload["transactions"]}
    assert {t.source.provider_id for t in transactions} == raw_ids


def test_ids_are_reconstructible_from_their_own_source_ref(accounts, transactions):
    for record in [*accounts, *transactions]:
        assert record.id == make_id(record.source)
        assert record.source.provider is Provider.PLAID
        assert record.source.connection_id == ITEM_ID


def test_transaction_account_ids_resolve_to_accounts(accounts, transactions):
    """The orphan check: both sides must be namespaced the same way."""
    known = {a.id for a in accounts}
    orphans = {t.account_id for t in transactions if t.account_id not in known}
    assert not orphans, f"{sorted(orphans)[:3]} resolve to no account"


def test_ids_are_unique(accounts, transactions):
    for records in (accounts, transactions):
        ids = [r.id for r in records]
        assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------

def test_currencies_are_iso_codes(accounts, transactions):
    for record in [*accounts, *transactions]:
        assert len(record.currency) == 3 and record.currency.isupper()


def test_a_non_iso_currency_is_refused_rather_than_defaulted():
    """Quietly substituting USD for a crypto balance would put it into the
    net-worth total as dollars."""
    with pytest.raises(ValueError, match="not ISO-4217"):
        to_account({
            "account_id": "a4", "type": "depository", "subtype": "checking",
            "name": "Crypto",
            "balances": {"current": 1.0, "iso_currency_code": None,
                         "unofficial_currency_code": "BTC"},
        }, ITEM_ID)


# ---------------------------------------------------------------------------
# Pagination
#
# The one part of the I/O half worth testing without a network, because the
# recorded Item cannot exercise it: 48 transactions against count=500 means
# `has_more` is false on the first page and the loop body runs exactly once.
# Every multi-page bug therefore survives both the fixture AND a live run
# against sandbox.
# ---------------------------------------------------------------------------

class _FakeSdkTransaction:
    """Stands in for a generated SDK model, which the adapter calls to_dict on."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def to_dict(self) -> dict:
        return self._payload


class _PagingClient:
    """A transactions_sync that hands back two pages, then stops.

    Records the cursor it was given on each call, so the test can assert the
    adapter threads it through rather than re-requesting page one forever --
    which would loop until the process died.
    """

    def __init__(self, pages, modified=None, removed=None) -> None:
        self._pages = pages
        self._modified = modified or [[] for _ in pages]
        self._removed = removed or [[] for _ in pages]
        self.cursors_seen: list[str | None] = []

    def transactions_sync(self, request):
        # The generated model only HAS a `cursor` attribute when one was
        # passed, so absence is the signal for "first page" -- which is
        # precisely what the adapter relies on.
        self.cursors_seen.append(getattr(request, "cursor", None))

        index = len(self.cursors_seen) - 1
        return {
            "added": [_FakeSdkTransaction(t) for t in self._pages[index]],
            "modified": [_FakeSdkTransaction(t) for t in self._modified[index]],
            "removed": self._removed[index],
            "next_cursor": f"cursor-{index + 1}",
            "has_more": index + 1 < len(self._pages),
        }


def _plaid_txn(transaction_id: str, day: int) -> dict:
    return {
        "transaction_id": transaction_id,
        "account_id": "acct_1",
        "date": f"2026-07-{day:02d}",
        "name": "COFFEE SHOP",
        "merchant_name": "Coffee Shop",
        "amount": 4.25,
        "iso_currency_code": "USD",
        "pending": False,
        "personal_finance_category": {"primary": "FOOD_AND_DRINK"},
    }


def test_fetch_transactions_follows_the_cursor_across_pages():
    """Concatenates pages and stops on has_more, not on a short page."""
    from adapters.plaid_source import PlaidSource

    pages = [
        [_plaid_txn("t1", 1), _plaid_txn("t2", 2)],
        [_plaid_txn("t3", 3)],                        # short page, NOT the end
        [_plaid_txn("t4", 4)],
    ]
    client = _PagingClient(pages)
    source = PlaidSource(access_token="tok", item_id=ITEM_ID, client=client)

    result = source.fetch_transactions()

    assert [t.source.provider_id for t in result.added] == ["t1", "t2", "t3", "t4"]
    # First request carries no cursor; each subsequent one carries the previous
    # response's next_cursor.
    assert client.cursors_seen == [None, "cursor-1", "cursor-2"]


def test_fetch_transactions_stops_after_one_page_when_has_more_is_false():
    """The path the real sandbox Item actually takes."""
    from adapters.plaid_source import PlaidSource

    client = _PagingClient([[_plaid_txn("t1", 1)]])
    source = PlaidSource(access_token="tok", item_id=ITEM_ID, client=client)

    assert len(source.fetch_transactions().added) == 1
    assert client.cursors_seen == [None]


def test_modified_transactions_are_translated_like_added_ones():
    """/transactions/sync reports an amended charge under `modified`, not
    `added`. Through 5.6 the adapter read only `added`, so a tip adjustment or
    a corrected amount was silently discarded and the stale row survived."""
    from adapters.plaid_source import PlaidSource

    client = _PagingClient(
        pages=[[_plaid_txn("t1", 1)]],
        modified=[[_plaid_txn("t2", 2)]],
    )
    source = PlaidSource(access_token="tok", item_id=ITEM_ID, client=client)
    result = source.fetch_transactions()

    assert [t.source.provider_id for t in result.added] == ["t1"]
    assert [t.source.provider_id for t in result.modified] == ["t2"]


def test_removed_ids_are_translated_into_our_id_space():
    """A bare Plaid transaction_id matches no row we ever stored -- every id in
    the database is provider:connection:provider_id. Handing the raw value up
    would produce a delete that silently matches nothing."""
    from adapters.plaid_source import PlaidSource
    from core.models import Provider, SourceRef, make_id

    client = _PagingClient(
        pages=[[_plaid_txn("t1", 1)]],
        removed=[[{"transaction_id": "gone_1"}]],
    )
    source = PlaidSource(access_token="tok", item_id=ITEM_ID, client=client)
    result = source.fetch_transactions()

    expected = make_id(SourceRef(
        provider=Provider.PLAID,
        provider_id="gone_1",
        connection_id=ITEM_ID,
    ))
    assert result.removed == [expected]
    assert result.removed[0].startswith("plaid:")


def test_the_cursor_survives_to_the_caller():
    """5.6 followed the cursor across pages and then threw it away, so every
    sync restarted from the beginning of time. Returning it is what makes 5.8
    possible."""
    from adapters.plaid_source import PlaidSource

    client = _PagingClient([[_plaid_txn("t1", 1)], [_plaid_txn("t2", 2)]])
    source = PlaidSource(access_token="tok", item_id=ITEM_ID, client=client)

    assert source.fetch_transactions().cursor == "cursor-2"


def test_no_cursor_replays_from_the_beginning():
    """The first sync, and the fallback whenever a cursor is unavailable.
    Passing None must fetch the Item's whole history, not nothing."""
    from adapters.plaid_source import PlaidSource

    client = _PagingClient([[_plaid_txn("t1", 1)]])
    source = PlaidSource(access_token="tok", item_id=ITEM_ID, client=client)
    source.fetch_transactions()

    assert client.cursors_seen == [None]


def test_a_stored_cursor_is_sent_on_the_first_request():
    """5.8's behaviour change. Without this the adapter refetched the entire
    Item on every sync -- correct, and increasingly expensive, and the reason
    /transactions/sync issues a cursor at all."""
    from adapters.plaid_source import PlaidSource

    client = _PagingClient([[_plaid_txn("t1", 1)]])
    source = PlaidSource(access_token="tok", item_id=ITEM_ID, client=client)
    source.fetch_transactions("cursor-from-last-sync")

    assert client.cursors_seen == ["cursor-from-last-sync"]


def test_a_resumed_fetch_still_follows_pagination():
    """Resuming and paginating are independent: a cursor that resumes into a
    multi-page window must still walk to the end, or the sync silently keeps
    only the first page of what changed."""
    from adapters.plaid_source import PlaidSource

    client = _PagingClient([[_plaid_txn("t1", 1)], [_plaid_txn("t2", 2)]])
    source = PlaidSource(access_token="tok", item_id=ITEM_ID, client=client)
    result = source.fetch_transactions("resume-here")

    assert client.cursors_seen == ["resume-here", "cursor-1"]
    assert len(result.added) == 2


def test_connection_names_the_item():
    from adapters.plaid_source import PlaidSource
    from core.models import ConnectionRef, Provider

    source = PlaidSource(access_token="tok", item_id=ITEM_ID)

    assert source.connection() == ConnectionRef(
        provider=Provider.PLAID, connection_id=ITEM_ID
    )


def test_removals_across_pages_all_survive():
    """Same accumulate-then-stop rule as `added`: a removal on page one is not
    dropped because page two also had one."""
    from adapters.plaid_source import PlaidSource

    client = _PagingClient(
        pages=[[_plaid_txn("t1", 1)], [_plaid_txn("t2", 2)]],
        removed=[[{"transaction_id": "gone_1"}], [{"transaction_id": "gone_2"}]],
    )
    source = PlaidSource(access_token="tok", item_id=ITEM_ID, client=client)

    assert len(source.fetch_transactions().removed) == 2


def test_an_access_token_is_required():
    from adapters.plaid_source import PlaidSource

    with pytest.raises(ValueError, match="needs an access_token"):
        PlaidSource(access_token="")


# ---------------------------------------------------------------------------
# raw
# ---------------------------------------------------------------------------

def test_raw_holds_the_whole_payload_not_a_hand_picked_key(payload,
                                                            transactions):
    """`Transaction.raw` is documented as "the provider's original payload".

    Storing only pending_transaction_id under that name would read as the full
    response to anyone trusting the model's docstring.
    """
    by_id = {t["transaction_id"]: t for t in payload["transactions"]}
    for txn in transactions:
        source_dict = by_id[txn.source.provider_id]
        assert txn.raw == source_dict
        # Equal but NOT the same object. Without this the assertion above is
        # vacuous -- it would compare a dict to itself and pass on an adapter
        # that aliased the caller's payload.
        assert txn.raw is not source_dict

    # And the key Step 5 needs is still reachable.
    assert all("pending_transaction_id" in t.raw for t in transactions)


def test_mutating_raw_cannot_corrupt_the_payload_it_came_from():
    """The failure the copy prevents, stated as behaviour.

    RecordedPlaidSource hands rows out of a module-scoped JSON payload, so an
    aliased `raw` means one annotation downstream silently rewrites the fixture
    for every later reader in the process -- including other tests.
    """
    source_dict = _plaid_txn("t1", 1)
    txn = to_transaction(source_dict, ITEM_ID)

    txn.raw["annotated_by_step_5"] = True
    assert "annotated_by_step_5" not in source_dict


def test_the_copy_is_shallow_and_that_is_documented_not_accidental():
    """Nested values are still shared. Asserted so the limit is a known
    property rather than a surprise the first time someone relies on it."""
    source_dict = _plaid_txn("t1", 1)
    txn = to_transaction(source_dict, ITEM_ID)

    assert txn.raw["personal_finance_category"] is source_dict[
        "personal_finance_category"]


def test_the_account_type_table_only_maps_to_real_account_types():
    """A typo'd value here would be an AttributeError at import; this pins that
    every entry is a member of the enum the rest of the app switches on."""
    assert all(isinstance(v, AccountType) for v in ACCOUNT_TYPES.values())
