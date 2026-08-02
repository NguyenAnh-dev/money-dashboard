"""
tests/test_conformance.py
-------------------------
THE ADAPTER CONTRACT, as executable assertions. Run from the project root:

    ./venv/Scripts/python.exe -m pytest tests/test_conformance.py

This is the file step 4.3 adopted pytest *for*. Every other test in this repo
knows which adapter it is testing; this one deliberately does not. It states
what `core/models.py` promises about ANY source -- ids namespaced and unique,
amounts integer, merchants present, debt negative -- and then runs the whole
list once per adapter, so a new provider inherits the entire suite by adding one
entry to ALL_SOURCES.

WHY THIS EXISTS SEPARATELY FROM tests/check_baseline.py
The gate's `check_structure()` asserts several of these same invariants, and
that duplication is deliberate. The gate is the release check: it renders in a
process it fully controls and must keep working as a standalone script with no
pytest involved. Importing shared assertions from here would couple it to the
test suite it is supposed to be independent of. The assertions are four lines
each; the gate's independence is worth more than the de-duplication.

The division of labour, so neither file grows into the other:
  * check_baseline.py -- does the CONFIGURED source still render byte-identically,
    and is its model intact? Fake only, by definition.
  * this file -- does EVERY adapter honour the contract, regardless of what
    DATA_SOURCE says and regardless of whether it renders at all.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Environment, pinned BEFORE the adapters are imported or constructed
#
# This module instantiates adapters directly rather than going through
# data_source.get_*, so DATA_SOURCE has no say in what gets tested -- but it is
# pinned anyway, because an adapter is free to consult it and a test that
# quietly changes meaning based on the shell it ran in is worse than no test.
#
# CSV_PATH is the one that actually bites: CsvSource() falls back to the
# RELATIVE "fixtures/bank_export.csv", so a bare CsvSource() only works when
# pytest happens to be invoked from the project root. Pinning an absolute path
# here is what lets ALL_SOURCES stay a plain list of no-argument constructors.
# ---------------------------------------------------------------------------

os.environ["DATA_SOURCE"] = "fake"
os.environ["CSV_PATH"] = str(ROOT / "fixtures" / "bank_export.csv")

import json                                        # noqa: E402
from adapters.csv_source import CsvSource          # noqa: E402  (after the pins)
from adapters.fake import FakeSource               # noqa: E402
from adapters.plaid_source import (                # noqa: E402
    to_account,
    to_transaction,
)
from core.models import (                          # noqa: E402
    DEBT_TYPES,
    AccountType,
    Provider,
    SourceRef,
    Transaction,
    _clean_currency,
    _escape_segment,
    _require_int_cents,
    make_id,
)

#: EVERY adapter. Adding a provider means adding one entry here and nothing
#: else -- the whole file below is parameterized over this list, so a new
#: source arrives already covered by the full contract.
#:
#: Instances, not classes, because constructor shapes differ: FakeSource takes
#: nothing, CsvSource takes a path and an account, PlaidSource an access token.
#: The list is the one place that knows how to build each one.
#:
#: PLAID BROKE THE "ADD ONE ENTRY" PROMISE, and it is worth saying why rather
#: than quietly working around it. FakeSource and CsvSource are pure: same
#: input, same output, no setup. PlaidSource needs credentials, a network, and
#: a token from a multi-step handshake, so it cannot simply be constructed here
#: and run. It participates as TWO entries instead:
#:
#:   * RecordedPlaidSource -- the adapter's translation driven by a committed
#:     sandbox payload. No credentials, no network, runs everywhere, every
#:     time. This is the one that actually enforces the contract, because every
#:     assertion below is a property of the TRANSLATION rather than of the
#:     connection.
#:   * The live PlaidSource -- appended only when credentials are present, and
#:     skipped loudly otherwise (see `_live_plaid_source`). It checks the one
#:     thing the recording cannot: that the I/O half still returns what the
#:     translation half expects.
#:
#: A skipped test reads as green, so a Plaid adapter that participated ONLY by
#: skipping would be unconformed in practice while the suite said nothing.


class RecordedPlaidSource:
    """The Plaid adapter's translation, driven by fixtures/plaid_sandbox.json.

    Deliberately NOT a subclass or a mock of PlaidSource. It reuses the real
    `to_account` / `to_transaction` -- the entire mapping under test -- and
    replaces only the network. Mocking the SDK instead would test our mock.
    """

    #: Any non-empty string. The payload does not record an item_id, and every
    #: id assertion rebuilds ids from each record's own SourceRef anyway.
    ITEM_ID = "recorded_sandbox_item"

    def __init__(self, path: Path) -> None:
        self._payload = json.loads(path.read_text(encoding="utf-8"))

    def fetch_accounts(self):
        return [to_account(raw, self.ITEM_ID)
                for raw in self._payload["accounts"]]

    def fetch_transactions(self):
        return [to_transaction(raw, self.ITEM_ID)
                for raw in self._payload["transactions"]]


PLAID_FIXTURE = ROOT / "fixtures" / "plaid_sandbox.json"

ALL_SOURCES = [FakeSource(), CsvSource()]

if PLAID_FIXTURE.exists():
    ALL_SOURCES.append(RecordedPlaidSource(PLAID_FIXTURE))

#: ISO-4217: exactly three upper-case letters. Anything else splits a currency
#: group in two or renders as a mangled symbol.
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _source_name(source) -> str:
    return type(source).__name__


parametrized = pytest.mark.parametrize(
    "source", ALL_SOURCES, ids=_source_name
)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

@parametrized
def test_fetch_methods_return_lists(source):
    """A list, not a generator and not a bare object.

    A generator would pass most assertions below on the first pass and then be
    silently empty on the second -- including the determinism check, which
    would compare a full list against nothing and could not distinguish that
    from a genuine match.
    """
    accounts = source.fetch_accounts()
    transactions = source.fetch_transactions()

    assert isinstance(accounts, list)
    assert isinstance(transactions, list)
    assert accounts, f"{_source_name(source)} returned no accounts"
    assert transactions, f"{_source_name(source)} returned no transactions"


# ---------------------------------------------------------------------------
# Ids
# ---------------------------------------------------------------------------

@parametrized
def test_account_ids_are_unique(source):
    ids = [a.id for a in source.fetch_accounts()]
    assert len(set(ids)) == len(ids)


@parametrized
def test_transaction_ids_are_unique_and_non_empty(source):
    """Uniqueness is what makes a re-sync an upsert instead of a duplicate.

    Note this is the assertion the CSV duplicate-row minter exists to keep
    true: two genuinely identical rows would otherwise hash to one id.
    """
    ids = [t.id for t in source.fetch_transactions()]
    assert all(ids), "every transaction needs a non-empty id"
    assert len(set(ids)) == len(ids), (
        f"{_source_name(source)}: {len(ids)} rows, {len(set(ids))} distinct ids"
    )


@parametrized
def test_every_transaction_account_id_resolves(source):
    """THE ORPHAN CHECK, and the reason check_baseline.py has a STRUCTURE half.

    The dashboard never joins a transaction to its account, so namespacing the
    accounts while leaving `Transaction.account_id` bare renders a completely
    byte-identical screen on a completely broken model. No diff can catch it.
    """
    account_ids = {a.id for a in source.fetch_accounts()}
    orphans = {t.account_id for t in source.fetch_transactions()
               if t.account_id not in account_ids}
    assert not orphans, (
        f"{_source_name(source)}: {sorted(orphans)[:3]} resolve to no account. "
        f"Known: {sorted(account_ids)[:3]}. One side namespaced and the other "
        f"not is the usual cause."
    )


@parametrized
def test_ids_are_exactly_what_make_id_produces(source):
    """The id must be reconstructible from the record's own SourceRef.

    Stated this way rather than by picking the id apart, because `core/models`
    says never to parse an id -- and because this is strictly stronger. It
    catches an adapter that mints a plausible-looking id from the wrong
    connection, which a prefix check would wave through.
    """
    for record in [*source.fetch_accounts(), *source.fetch_transactions()]:
        assert record.id == make_id(record.source), (
            f"{record.id!r} does not match its own provenance "
            f"{record.source!r}"
        )


@parametrized
def test_ids_have_three_unescaped_segments(source):
    """The format contract: provider:connection_id:provider_id, and no more.

    This is the ONE place in the codebase that splits an id, and it is doing so
    to assert the format rather than to read data out of it. The property being
    checked is that escaping worked -- a raw ':' inside a segment would show up
    here as a fourth part, which is exactly the collision `_escape_segment`
    exists to prevent (connection "a:b" + id "c" vs connection "a" + id "b:c").
    """
    for record in [*source.fetch_accounts(), *source.fetch_transactions()]:
        segments = record.id.split(":")
        assert len(segments) == 3, (
            f"{record.id!r} has {len(segments)} segments; a raw colon survived "
            f"escaping"
        )
        assert all(segments), f"{record.id!r} has a blank segment"

        provider, connection, _ = segments
        assert provider == record.source.provider.value
        assert connection == _escape_segment(
            record.source.connection_id, "connection_id"
        )


@parametrized
def test_two_fetches_produce_identical_ids(source):
    """THE DETERMINISM CHECK. Nothing visible depends on it today; at Step 5 it
    is the difference between an import that updates rows and one that inserts
    a second copy of the entire statement.

    Compared as ORDERED lists, so an adapter whose ordering wobbles between
    calls fails here rather than surviving on set equality.
    """
    first = [t.id for t in source.fetch_transactions()]
    second = [t.id for t in source.fetch_transactions()]
    assert first == second

    assert ([a.id for a in source.fetch_accounts()]
            == [a.id for a in source.fetch_accounts()])


# ---------------------------------------------------------------------------
# Field invariants
# ---------------------------------------------------------------------------

@parametrized
def test_amounts_are_integer_minor_units(source):
    """Invariant 1. `bool` is checked explicitly because isinstance(True, int)
    is True in Python, so a boolean amount would sail through a naive check and
    then behave as 1 cent."""
    values = [a.balance_cents for a in source.fetch_accounts()]
    values += [t.amount_cents for t in source.fetch_transactions()]

    for value in values:
        assert not isinstance(value, bool), f"{value!r} is a bool, not money"
        assert type(value) is int, (
            f"{value!r} is {type(value).__name__}; money is integer minor "
            f"units. int(amount * 100) is the bug this catches."
        )


@parametrized
def test_merchant_is_never_empty(source):
    """Invariant 4. Grouping happens on merchant, so a blank one creates a
    silent catch-all bucket that swallows every row that has it."""
    for txn in source.fetch_transactions():
        assert txn.merchant and txn.merchant.strip(), (
            f"{txn.id}: merchant is {txn.merchant!r} (raw name {txn.name!r})"
        )


@parametrized
def test_debt_accounts_are_not_positive(source):
    """Debt is negative in our model. A provider that reports a card balance as
    positive (Plaid does) needs the flip in ITS adapter -- and getting it wrong
    moves net worth by twice the balance.

    `> 0`, not `>= 0`: a paid-off card sits at exactly zero and that is fine.
    """
    for account in source.fetch_accounts():
        if account.type in DEBT_TYPES:
            assert account.balance_cents <= 0, (
                f"{account.id}: debt account holds {account.balance_cents}; "
                f"net worth would be off by {2 * account.balance_cents}"
            )


@parametrized
def test_currency_is_a_valid_iso_code(source):
    for record in [*source.fetch_accounts(), *source.fetch_transactions()]:
        assert _CURRENCY_RE.match(record.currency), (
            f"{record.id}: currency {record.currency!r} is not a 3-letter "
            f"ISO-4217 code"
        )


@parametrized
def test_dates_are_date_objects_not_strings_or_datetimes(source):
    """A `datetime` is the trap here, not a string -- a string fails loudly on
    the first comparison, but `datetime` IS a `date` subclass, so it passes
    isinstance and then carries a time component that jitters every gap
    calculation in find_recurring."""
    for txn in source.fetch_transactions():
        assert isinstance(txn.date, date), (
            f"{txn.id}: date is {type(txn.date).__name__}"
        )
        assert not isinstance(txn.date, datetime), (
            f"{txn.id}: date is a datetime; the time component will perturb "
            f"day-gap arithmetic"
        )


@parametrized
def test_pending_is_a_bool(source):
    for txn in source.fetch_transactions():
        assert isinstance(txn.pending, bool)


# ---------------------------------------------------------------------------
# Cross-adapter -- run once, not per source
#
# These cannot be parameterized by construction: they are about the
# RELATIONSHIP between adapters, which no single source can assert about
# itself.
# ---------------------------------------------------------------------------

def _all_ids(source) -> set[str]:
    return ({a.id for a in source.fetch_accounts()}
            | {t.id for t in source.fetch_transactions()})


def test_no_id_collides_between_adapters():
    """Invariant 3, checked as the property it actually is.

    Two sources CAN mint the same provider_id -- the fixture and the CSV both
    hash content with the same algorithm, so a genuine digest collision is not
    absurd. Namespacing is what makes it harmless, and that is what this
    asserts: not that the digests differ, but that the full ids cannot meet.
    """
    ids = [_all_ids(source) for source in ALL_SOURCES]
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            assert not (left & right), f"colliding ids: {sorted(left & right)[:3]}"


@parametrized
def test_every_id_carries_its_own_providers_prefix(source):
    """The other half of namespacing: an adapter must not mint under a provider
    that is not its own. A copy-paste of fake.py that forgot to change PROVIDER
    would pass every uniqueness check in this file and fail here."""
    providers = {a.source.provider for a in source.fetch_accounts()}
    providers |= {t.source.provider for t in source.fetch_transactions()}
    assert len(providers) == 1, f"{_source_name(source)} mints under {providers}"

    (provider,) = providers
    prefix = f"{provider.value}:"
    assert all(i.startswith(prefix) for i in _all_ids(source))


def test_the_source_list_covers_every_provider_that_has_an_adapter():
    """The list is the registry, so it has to be caught when it goes stale.

    Provider is the enum of everything we CAN read; ALL_SOURCES is everything
    we currently do. PLAID is expected to be missing until its adapter lands --
    naming the gap explicitly means the day it arrives, this test is what tells
    you to add it to ALL_SOURCES rather than leaving it silently unconformed.
    """
    covered = {type(s).__name__: s for s in ALL_SOURCES}
    assert len(covered) == len(ALL_SOURCES), "ALL_SOURCES has a duplicate class"

    tested = {next(iter({t.source.provider for t in s.fetch_transactions()}))
              for s in ALL_SOURCES}

    not_yet = set(Provider) - tested
    assert not not_yet, (
        f"{not_yet} has no adapter in ALL_SOURCES. Add it, or update this "
        f"assertion to say why it is exempt."
    )


def test_the_plaid_fixture_is_present_so_plaid_is_actually_covered():
    """Separate from the registry check above, and deliberately so.

    ALL_SOURCES only picks up RecordedPlaidSource when the fixture exists, so
    a missing fixture would silently drop Plaid from the entire contract and
    the registry test would then fail with a confusing message about the enum.
    This states the real precondition: the recording is what makes Plaid
    conformed, and losing it is a specific, fixable problem.
    """
    assert PLAID_FIXTURE.exists(), (
        f"{PLAID_FIXTURE.name} is missing, so the Plaid adapter is running "
        f"unconformed. Re-record it: "
        f"./venv/Scripts/python.exe scripts/get_plaid_token.py --record-only"
    )


# ---------------------------------------------------------------------------
# core/models edge cases
#
# Deferred from step 4.2 because nothing exercised them; the conformance suite
# above now depends on all four holding, so they get pinned here. Each one is a
# guard whose failure mode is silent data loss rather than a crash.
# ---------------------------------------------------------------------------

def test_make_id_is_injective_across_the_segment_boundary():
    """The collision plain concatenation would produce:

        connection_id="a:b", provider_id="c"   -> plaid:a:b:c
        connection_id="a",   provider_id="b:c" -> plaid:a:b:c

    Two distinct records, one key, and at Step 5 one silently loses the upsert.
    """
    left = make_id(SourceRef(Provider.PLAID, provider_id="c", connection_id="a:b"))
    right = make_id(SourceRef(Provider.PLAID, provider_id="b:c", connection_id="a"))

    assert left != right
    # Both must still be three segments -- the escape is what buys that.
    assert len(left.split(":")) == 3
    assert len(right.split(":")) == 3


def test_escaping_percent_before_colon_keeps_it_injective():
    """Order matters in `_escape_segment`. Escape ':' first and the literal
    input "a%3Ab" and the escaped form of "a:b" both render as "a%3Ab",
    reintroducing the collision being removed."""
    assert _escape_segment("a:b", "x") != _escape_segment("a%3Ab", "x")


@pytest.mark.parametrize("segment", ["", None])
def test_escape_segment_rejects_empty(segment):
    """An id like "fake::acc_1" means an adapter forgot connection_id. Better a
    loud failure than a row keyed on a blank."""
    with pytest.raises(ValueError, match="must not be empty"):
        _escape_segment(segment, "connection_id")


def test_make_id_rejects_a_blank_connection_id():
    with pytest.raises(ValueError, match="must not be empty"):
        make_id(SourceRef(Provider.CSV, provider_id="row_1", connection_id=""))


# ---------------------------------------------------------------------------
# The LIVE Plaid source
#
# Everything above runs on a recording. This is the only thing here that talks
# to Plaid, and it exists to catch the one class of failure a recording cannot:
# the I/O half drifting away from the translation half -- a renamed response
# field, an SDK upgrade that changes what to_dict() emits, an expired grant.
#
# Skipped when credentials are absent, which is most of the time. That is
# acceptable ONLY because the recorded source above already enforces the
# contract on every run; a skip here loses the freshness check, not the
# contract.
# ---------------------------------------------------------------------------

def _plaid_credentials_present() -> bool:
    return all(os.environ.get(name, "").strip() for name in
               ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ACCESS_TOKEN"))


requires_plaid_credentials = pytest.mark.skipif(
    not _plaid_credentials_present(),
    reason="PLAID_CLIENT_ID / PLAID_SECRET / PLAID_ACCESS_TOKEN not set; "
           "the recorded fixture covers the contract, this covers freshness",
)


@requires_plaid_credentials
def test_the_live_plaid_source_still_honours_the_contract():
    """A compact re-run of the invariants, against the real API.

    Not parameterized into ALL_SOURCES, because that list is built at import
    and a network call per test would turn a 0.2s suite into a slow, flaky one
    for whoever does have credentials exported.
    """
    from adapters.plaid_source import PlaidSource

    source = PlaidSource.from_env()
    accounts = source.fetch_accounts()
    transactions = source.fetch_transactions()

    assert accounts and transactions

    known = {a.id for a in accounts}
    assert not {t.account_id for t in transactions if t.account_id not in known}

    for record in [*accounts, *transactions]:
        assert record.id == make_id(record.source)
        assert record.source.provider is Provider.PLAID
        assert type(getattr(record, "balance_cents",
                            getattr(record, "amount_cents", 0))) is int

    for txn in transactions:
        assert txn.merchant and txn.merchant.strip()
        # `type(...) is date`, not isinstance: datetime subclasses date, so an
        # isinstance check cannot see one slipping through.
        assert type(txn.date) is date

    for account in accounts:
        if account.type in DEBT_TYPES:
            assert account.balance_cents <= 0


@pytest.mark.parametrize("currency", ["US", "USDD", "", "U$D", "12", "1 2", "ÉUR"])
def test_clean_currency_rejects_non_iso_codes(currency):
    """"US" is the trap a reader spots; the rest are the ones a LENGTH-ONLY
    check waved through until this test was written. "U$D" and "ÉUR" are both
    three characters, and either would sit beside the real code as a separate
    currency group that reads identically in a rendered table."""
    with pytest.raises(ValueError, match="3-letter ISO-4217"):
        _clean_currency(currency)


@pytest.mark.parametrize("currency, expected", [("usd", "USD"), (" eur ", "EUR")])
def test_clean_currency_normalizes(currency, expected):
    """Case and whitespace normalized, so 'usd' and 'USD' never split a group."""
    assert _clean_currency(currency) == expected


@pytest.mark.parametrize("value", [12.34, 0.0, True, False, "1234", None])
def test_require_int_cents_rejects_everything_that_is_not_an_int(value):
    """float because 10.99 is really 10.990000000000000213..., and
    int(10.99 * 100) is 1098 -- a penny gone in silence. bool because
    isinstance(True, int) is True and it would quietly become 1 cent."""
    with pytest.raises(TypeError):
        _require_int_cents(value, "amount_cents")


def test_the_model_actually_enforces_those_guards_on_construction():
    """The guards matter only if `__post_init__` runs them. Pinned through a
    real Transaction so a refactor that drops the call is caught here."""
    ref = SourceRef(Provider.CSV, provider_id="row_1", connection_id="batch")
    kwargs = dict(
        id=make_id(ref), source=ref, account_id="csv:batch:acct",
        date=date(2026, 5, 1), name="RAW", merchant="M", category="C",
    )

    with pytest.raises(TypeError):
        Transaction(amount_cents=12.34, **kwargs)
    with pytest.raises(ValueError):
        Transaction(amount_cents=1234, currency="US", **kwargs)

    ok = Transaction(amount_cents=1234, currency="usd", **kwargs)
    assert ok.currency == "USD"


def test_debt_types_is_what_the_conformance_check_reads():
    """DEBT_TYPES drives test_debt_accounts_are_not_positive. If a new
    liability type is added to AccountType and not to DEBT_TYPES, that check
    silently stops covering it."""
    assert DEBT_TYPES == {AccountType.CREDIT, AccountType.LOAN}
