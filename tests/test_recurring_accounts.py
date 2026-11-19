from __future__ import annotations

import hashlib
import io
import itertools
import re
from datetime import date
from pathlib import Path

from rich.console import Console

from adapters.csv_source import CsvSource
from core.models import (
    Account, AccountType, Provider, SourceRef, Transaction, TransactionKind,
    make_id,
)
from insights import find_recurring
from main import account_labels, show_subscriptions

FIXTURE = str(Path(__file__).resolve().parent.parent / "fixtures" / "bank_export.csv")

MONTHS = [date(2026, 5, 10), date(2026, 6, 10), date(2026, 7, 11)]


def _account(tag: str) -> Account:
    ref = SourceRef(Provider.FAKE, f"acc_{tag}", "conn")
    return Account(id=make_id(ref), source=ref, name=f"Card {tag}",
                   institution="Bank", type=AccountType.CREDIT, balance_cents=0)


def _charges(account, merchant, dates, cents=1799, **kwargs):
    out = []
    for i, d in enumerate(dates):
        ref = SourceRef(Provider.FAKE, f"{account.source.provider_id}_{merchant}_{i}_{d}", "conn")
        fields = dict(id=make_id(ref), source=ref, account_id=account.id, date=d,
                      name=merchant, merchant=merchant, amount_cents=cents,
                      category="Entertainment")
        fields.update(kwargs)
        out.append(Transaction(**fields))
    return out


def test_three_monthly_charges_on_one_card_make_one_candidate_with_evidence():
    card = _account("a")
    found = find_recurring(_charges(card, "Netflix", MONTHS))

    assert len(found) == 1
    assert found[0].account_id == card.id
    assert found[0].evidence == tuple((d, 1799) for d in MONTHS)


def test_two_cards_cannot_pool_their_histories():
    a, b = _account("a"), _account("b")
    txns = _charges(a, "Netflix", MONTHS[:2]) + _charges(b, "Netflix", MONTHS[2:])

    assert len(txns) == 3
    assert find_recurring(txns) == []


def test_two_cards_with_enough_history_each_get_their_own_candidate():
    a, b = _account("a"), _account("b")
    txns = (_charges(a, "Netflix", MONTHS, cents=1799)
            + _charges(b, "Netflix", MONTHS, cents=1599))

    found = find_recurring(txns)

    assert len(found) == 2
    assert {c.account_id: c.latest_cents for c in found} == {a.id: 1799, b.id: 1599}
    assert all(c.occurrences == 3 for c in found)


def test_the_same_month_on_two_cards_is_not_two_charges_in_one_month():
    a, b = _account("a"), _account("b")
    txns = _charges(a, "Netflix", MONTHS) + _charges(b, "Netflix", MONTHS)

    assert [c.occurrences for c in find_recurring(txns)] == [3, 3]


def test_zero_pending_credit_and_transfer_entries_are_not_charges():
    card = _account("a")
    base = _charges(card, "Netflix", MONTHS[:2])
    for extra in (
        dict(amount_cents=0),
        dict(amount_cents=-1799),
        dict(pending=True),
        dict(kind=TransactionKind.TRANSFER),
    ):
        third = _charges(card, "Netflix", MONTHS[2:], **extra)
        assert find_recurring(base + third) == [], extra
    assert len(find_recurring(base + _charges(card, "Netflix", MONTHS[2:]))) == 1


def test_the_sample_csv_still_yields_hulu_with_its_price_rise():
    found = find_recurring(CsvSource(path=FIXTURE).fetch_transactions().added)

    assert [c.merchant for c in found] == ["Hulu"]
    assert found[0].latest_cents == 1999
    assert found[0].delta_cents == 200
    assert len(found[0].evidence) == 3


def _render(txns, accounts):
    out = io.StringIO()
    show_subscriptions(txns, Console(file=out, width=200, color_system=None), accounts)
    return out.getvalue()


def test_the_cli_table_names_the_account_and_lists_every_charge():
    a, b = _account("a"), _account("b")
    amounts = [1799, 1799, 1999]
    txns = []
    for card in (a, b):
        for i, (d, cents) in enumerate(zip(MONTHS, amounts)):
            txns += _charges(card, "Netflix", [d], cents=cents)

    text = _render(txns, [a, b])

    assert "Card a" in text and "Card b" in text
    assert text.count("Netflix") == 2
    for d, cents in zip(MONTHS, amounts):
        assert text.count(f"{d} ${cents / 100:.2f}") == 2
    assert text.count("+$2.00") == 2
    history = re.findall(r"(\d{4}-\d\d-\d\d) \$\d+\.\d\d", text)
    assert history == ["2026-05-10", "2026-06-10", "2026-07-11"] * 2


def _twin(tag, mask):
    ref = SourceRef(Provider.FAKE, f"acc_{tag}", "conn")
    return Account(id=make_id(ref), source=ref, name="Visa [Main]",
                   institution="Bank", type=AccountType.CREDIT,
                   balance_cents=0, mask=mask)


def test_same_name_accounts_are_told_apart_by_mask():
    a, b = _twin("a", "1111"), _twin("b", "2222")
    txns = _charges(a, "Netflix", MONTHS) + _charges(b, "Netflix", MONTHS)

    text = _render(txns, [a, b])

    assert "Visa [Main] (1111)" in text
    assert "Visa [Main] (2222)" in text
    assert "[id:" not in text


def test_same_name_same_mask_or_no_mask_falls_back_to_the_account_id():
    for masks in (("1111", "1111"), (None, None), ("1111", None)):
        a, b = _twin("a", masks[0]), _twin("b", masks[1])
        txns = _charges(a, "Netflix", MONTHS) + _charges(b, "Netflix", MONTHS)

        labels = account_labels([a, b])
        text = _render(txns, [a, b])
        again = account_labels([b, a])

        if masks == ("1111", None):
            assert "[id:" not in labels[a.id] + labels[b.id]
        else:
            assert labels[a.id] != labels[b.id]
            assert "[id:" in labels[a.id] and "[id:" in labels[b.id]
        assert labels[a.id] != labels[b.id]
        assert labels == again
        assert labels[a.id] in text and labels[b.id] in text
        assert "Visa [Main]" in text


def test_a_unique_name_is_left_alone_even_when_it_has_a_mask():
    a = _twin("a", "1111")
    assert account_labels([a]) == {a.id: "Visa [Main]"}


def _named(tag, name, mask=None):
    ref = SourceRef(Provider.FAKE, tag, "conn")
    return Account(id=make_id(ref), source=ref, name=name, institution="Bank",
                   type=AccountType.CREDIT, balance_cents=0, mask=mask)


def _assert_distinct_rows(accounts):
    txns = []
    for a in accounts:
        txns += _charges(a, "Netflix", MONTHS)
    out = io.StringIO()
    show_subscriptions(txns, Console(file=out, width=400, color_system=None), accounts)
    text = out.getvalue()
    labels = account_labels(accounts)

    assert len(find_recurring(txns)) == len(accounts) >= 2
    assert len(set(labels.values())) == len(accounts)
    assert text.count("Netflix") == len(accounts)
    for a in accounts:
        assert labels[a.id] in text
    for order in itertools.permutations(accounts):
        assert account_labels(list(order)) == labels
    return labels


def test_a_generated_label_cannot_match_another_accounts_literal_name():
    a = _named("a", "Checking", "1111")
    b = _named("b", "Checking", "2222")
    c = _named("c", "Checking (1111)")

    labels = _assert_distinct_rows([a, b, c])

    assert labels[b.id] == "Checking (2222)"
    assert "[id:" in labels[a.id] and "[id:" in labels[c.id]
    assert a.id in labels[a.id] and c.id in labels[c.id]


def _short_hash_collision():
    seen = {}
    for n in itertools.count():
        account_id = f"fake:conn:acc{n}"
        key = hashlib.sha1(account_id.encode("utf-8")).hexdigest()[:6]
        if key in seen:
            return seen[key], account_id
        seen[key] = account_id


def test_ids_that_collide_on_the_old_six_character_hash_stay_distinct():
    first_id, second_id = _short_hash_collision()
    accounts = []
    for account_id in (first_id, second_id):
        tag = account_id.split(":")[-1]
        accounts.append(_named(tag, "Visa [Main]", "1111"))

    labels = _assert_distinct_rows(accounts)

    assert labels[accounts[0].id] != labels[accounts[1].id]
    assert all(a.id in labels[a.id] for a in accounts)


def test_labels_do_not_depend_on_input_order_or_the_set_of_other_accounts():
    accounts = [_named("a", "Visa", "1"), _named("b", "Visa", "1"),
                _named("c", "Amex"), _named("d", "Visa", "2")]
    expected = account_labels(accounts)

    for order in itertools.permutations(accounts):
        assert account_labels(order) == expected
    assert expected[accounts[3].id] == "Visa (2)"
    assert expected[accounts[2].id] == "Amex"


def test_names_differing_only_by_trailing_space_render_distinct_rows_at_80_columns():
    a = _named("a", "Checking")
    b = _named("b", "Checking ")
    assert a.name != b.name
    txns = _charges(a, "Netflix", MONTHS) + _charges(b, "Netflix", MONTHS)
    out = io.StringIO()

    show_subscriptions(txns, Console(file=out, width=80, color_system=None), [a, b])

    labels = account_labels([a, b])
    text = out.getvalue()
    assert len(find_recurring(txns)) == 2
    assert labels[a.id] != labels[b.id]
    assert all(label == label.rstrip() for label in labels.values())
    assert all("[id:" in label for label in labels.values())
    cells = []
    for ln in text.splitlines():
        parts = ln.split("│")
        if len(parts) < 7:
            continue
        if parts[1].strip() == "Netflix":
            cells.append("")
        if cells:
            cells[-1] += parts[2].strip()
    assert len(cells) == 2 and cells[0] != cells[1]
    assert all(c.startswith("Checking [id:") for c in cells)


def test_a_mask_still_separates_names_that_differ_only_by_trailing_space():
    a = _named("a", "Checking", "1111")
    b = _named("b", "Checking ", "2222")

    assert account_labels([a, b]) == {a.id: "Checking (1111)", b.id: "Checking (2222)"}
