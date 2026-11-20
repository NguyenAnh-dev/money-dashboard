from __future__ import annotations

from datetime import date
from io import StringIO

from rich.console import Console

import pytest

import main as app
from adapters import merchant as merchant_module
from adapters.csv_source import CsvAccount, CsvSource, ParsedRow
from adapters.plaid_source import to_account as plaid_account
from adapters.merchant import normalize_merchant
from core.models import Account, AccountType, Provider, SourceRef, make_id
from tests.test_price_increases import (
    EXAMPLE, env, load, query, render, track,
)
from tests.test_render import _render, _seed
from core import repository, store, users


def csv_source(name, rows, balance=0):
    return CsvSource(rows=[ParsedRow(date.fromisoformat(d), desc, cents)
                           for d, desc, cents in rows],
                     account=CsvAccount(provider_id=name, name=name,
                                        balance_cents=balance),
                     connection_id=f"conn-{name}")


def seed_csv(path, *sources):
    accounts, transactions = [], []
    for source in sources:
        accounts += source.fetch_accounts()
        transactions += source.fetch_transactions().added
    _seed(path, accounts, transactions)


def known_account(balance_cents, name="Savings"):
    ref = SourceRef(provider=Provider.FAKE, provider_id=name, connection_id="c")
    return Account(id=make_id(ref), source=ref, name=name, institution="Bank",
                   type=AccountType.SAVINGS, balance_cents=balance_cents)


def test_credit_and_charge_are_labelled_in_plain_text(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    seed_csv(path, csv_source("card", [
        ("2026-07-01", "ACME CORP PAYROLL", 230000 * -1),
        ("2026-07-02", "SAFEWAY #1", 5000),
    ]))
    out = _render(path, monkeypatch)
    payroll = next(l for l in out.splitlines() if "Payroll Deposit" in l)
    safeway = next(l for l in out.splitlines() if "Safeway" in l)
    assert "Credit" in payroll and "Charge" not in payroll
    assert "Charge" in safeway and "Credit" not in safeway
    assert "$2300.00" in payroll and "$50.00" in safeway


def test_csv_only_account_shows_unknown_balance_and_unavailable_net_worth(
        tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    seed_csv(path, csv_source("card", [("2026-07-02", "SAFEWAY #1", 5000)],
                              balance=777700))
    out = _render(path, monkeypatch)
    assert "unknown" in out
    assert "unavailable" in out
    assert "Excluded (balance unknown): card" in out
    assert "$7777.00" not in out


def test_known_zero_balance_is_shown_as_zero_not_unknown(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    seed_csv(path)
    _seed(path, [known_account(0)], [])
    out = _render(path, monkeypatch)
    assert "$0.00" in out
    assert "unknown" not in out and "unavailable" not in out
    assert "Net Worth (across all banks)" in out


def test_mixed_balances_subtotal_names_included_and_excluded(
        tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    csv = csv_source("card", [("2026-07-02", "SAFEWAY #1", 5000)],
                     balance=999900)
    accounts = [known_account(250000)] + csv.fetch_accounts()
    _seed(path, accounts, csv.fetch_transactions().added)
    out = _render(path, monkeypatch)
    assert "Net Worth (known balances only)" in out
    assert "$2500.00" in out and "$12499.00" not in out
    assert "Included: Savings" in out
    assert "Excluded (balance unknown): card" in out


def test_empty_states_tell_a_new_user_what_to_do(tmp_path, monkeypatch):
    absent = _render(tmp_path / "absent.db", monkeypatch)
    assert "import_csv.py FILE --account ACCOUNT_ID" in absent
    assert "python sync.py" in absent

    path = tmp_path / "t.db"
    seed_csv(path, csv_source("card", [
        ("2026-06-12", "NETFLIX.COM", 1599),
        ("2026-07-12", "NETFLIX.COM", 1599),
    ]))
    out = _render(path, monkeypatch)
    section = out.split("Recurring Charges")[1].split("Accounts")[0]
    assert "at least 3 different months" in section
    assert "Netflix" not in section
    assert "Netflix" in out.split("Recent Transactions")[1]
    assert "No price increases or upcoming renewals" in out
    assert "Price Increases" not in out


def test_increase_comes_before_subscriptions_before_accounts(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    out = render(path)
    price = out.index("Price Increases")
    recurring = out.index("Recurring Charges")
    accounts = out.index("Accounts")
    assert 0 < price < recurring < accounts
    assert "No price increases or upcoming renewals" not in out
    assert "+$2.00" in out.split("Recurring Charges")[0]


def test_netflix_descriptor_normalises_without_moving_ids(monkeypatch):
    rows = [ParsedRow(date(2026, 8, 12), "NETFLIX.COM", 1599)]

    def ids():
        source = CsvSource(rows=rows, connection_id="c")
        return [(t.id, t.name, t.merchant)
                for t in source.fetch_transactions().added]

    [(new_id, new_name, new_merchant)] = ids()
    assert normalize_merchant("NETFLIX.COM") == "Netflix"
    assert new_name == "NETFLIX.COM"

    monkeypatch.delitem(merchant_module.MERCHANT_PREFIXES, "NETFLIX")
    [(old_id, old_name, old_merchant)] = ids()
    assert old_merchant == "Netflix.Com" and new_merchant != old_merchant
    assert old_id == new_id and old_name == new_name


def test_reimport_updates_the_stored_merchant_in_place(env):
    _, path = env
    with pytest.MonkeyPatch.context() as patch:
        patch.delitem(merchant_module.MERCHANT_PREFIXES, "NETFLIX")
        load(env, "a.csv", EXAMPLE)
    before = query(path, "SELECT id, name, merchant FROM transactions ORDER BY id")
    assert {m for _, _, m in before} == {"Netflix.Com"}
    load(env, "b.csv", EXAMPLE)
    after = query(path, "SELECT id, name, merchant FROM transactions ORDER BY id")
    assert len(after) == 3
    assert [(i, n) for i, n, _ in after] == [(i, n) for i, n, _ in before]
    assert {m for _, _, m in after} == {"Netflix"}


def plaid_raw(name, current, kind="depository", subtype="checking"):
    balances = {"iso_currency_code": "USD"}
    if current is not None:
        balances["current"] = current
    return {"account_id": name, "type": kind, "subtype": subtype,
            "name": name, "balances": balances}


def test_plaid_missing_balance_is_flagged_and_a_genuine_zero_is_not():
    missing = plaid_account(plaid_raw("Brokerage", None, "investment", "brokerage"), "item")
    zero = plaid_account(plaid_raw("Checking", 0.0), "item")
    assert missing.balance_cents == zero.balance_cents == 0
    assert missing.balance_known is False and zero.balance_known is True


def test_all_known_balances_keep_the_plain_net_worth_panel(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    _seed(path, [known_account(250000), known_account(-5000, "Card")], [])
    out = _render(path, monkeypatch)
    assert "Net Worth (across all banks)" in out and "$2450.00" in out
    assert "unknown" not in out and "Excluded" not in out


def test_csv_only_and_missing_plaid_balances_are_unknown_in_the_renderer(
        tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    plaid = plaid_account(plaid_raw("Brokerage", None, "investment", "brokerage"), "item")
    zero = plaid_account(plaid_raw("Checking", 0.0), "item")
    conn = store.open_db(str(path))
    try:
        user = users.ensure_local_user(conn)
        from core.models import ConnectionRef
        repository.save(conn, user, [plaid, zero], [],
                        connection=ConnectionRef(provider=Provider.PLAID,
                                                 connection_id="item"))
    finally:
        conn.close()
    stored = {a.name: a.balance_known for a in _load_accounts(path)}
    assert stored == {"Brokerage": False, "Checking": True}

    out = _render(path, monkeypatch)
    brokerage = next(l for l in out.splitlines() if "Brokerage" in l)
    checking = next(l for l in out.splitlines() if "Checking" in l and "│" in l and "Excluded" not in l and "Included" not in l)
    assert "unknown" in brokerage and "$0.00" not in brokerage
    assert "$0.00" in checking
    assert "Net Worth (known balances only)" in out
    assert "Included: Checking" in out
    assert "Excluded (balance unknown): Brokerage" in out


def _load_accounts(path):
    conn = store.open_db(str(path))
    try:
        return repository.load_accounts(conn, users.LOCAL_USER_ID)
    finally:
        conn.close()


def test_net_worth_panel_labels_cannot_forge_lines(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    hostile = "Card" + chr(10) + "Excluded (balance unknown): X" + chr(27) + "[31m"
    csv = csv_source(hostile, [("2026-07-02", "SAFEWAY #1", 5000)])
    _seed(path, [known_account(100)] + csv.fetch_accounts(),
          csv.fetch_transactions().added)
    out = _render(path, monkeypatch)
    panel = out.split("Net Worth (known balances only)")[1].split("Spending")[0]
    assert chr(27) not in out
    starts = [l.strip("│ ").startswith("Excluded") for l in panel.splitlines()]
    assert sum(starts) == 1
    flat = panel.replace(chr(10), "")
    assert r"\x0a" in flat and r"\x1b" in flat



def test_alerts_come_first_and_tracked_precedes_a_higher_priced_candidate(
        tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    csv = csv_source("card", [
        ("2026-08-12", "NETFLIX.COM", 1599),
        ("2026-09-12", "NETFLIX.COM", 1599),
        ("2026-10-12", "NETFLIX.COM", 1799),
        ("2026-08-03", "BIGGYM MEMBERSHIP", 9999),
        ("2026-09-03", "BIGGYM MEMBERSHIP", 9999),
        ("2026-10-03", "BIGGYM MEMBERSHIP", 9999),
    ])
    seed_csv(path, csv)
    conn = store.open_db(str(path))
    try:
        user = users.ensure_local_user(conn)
        account = conn.execute("SELECT id FROM accounts").fetchone()[0]
        repository.set_decision(conn, user, account, "Netflix", repository.TRACKED)
    finally:
        conn.close()
    monkeypatch.setenv("DB_PATH", str(path))
    buffer = StringIO()
    app.main(console=Console(file=buffer, width=200, color_system=None,
                             force_terminal=False), today=date(2026, 11, 8))
    out = buffer.getvalue()

    increases = out.index("Price Increases")
    renewals = out.index("Upcoming Renewals")
    recurring = out.index("Recurring Charges")
    assert 0 < increases < renewals < recurring
    section = out[recurring:out.index("Accounts")]
    tracked = section.index("Netflix (tracked)")
    candidate = section.index("Biggym Membership")
    assert tracked < candidate
    assert "(tracked)" not in section[candidate:]
    assert "+$2.00" in out[increases:renewals]
    assert "2026-11-12" in out[renewals:recurring]
