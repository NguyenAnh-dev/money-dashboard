from __future__ import annotations

from datetime import date
from io import StringIO

from rich.console import Console

import main as app
from core import repository, store
from insights import estimate_renewals, next_billing_date
from tests.test_price_increases import (
    EXAMPLE, MERCHANT, d, env, load, query, track,
)

NOV5 = date(2026, 11, 5)


def dashboard(path, today):
    buffer = StringIO()
    app.main(console=Console(file=buffer, width=200, color_system=None,
                             force_terminal=False), today=today)
    out = buffer.getvalue()
    assert "Recurring Charges" in out
    return out


def renewals(out):
    if "Upcoming Renewals" not in out:
        return ""
    return out.split("Upcoming Renewals")[1].split("Spending by Category")[0]


def test_example_on_nov_5_estimates_nov_12_at_latest_amount(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    section = renewals(dashboard(path, NOV5))
    assert "2026-11-12" in section and "$17.99" in section
    assert "2026-10-12" in section
    assert "import updated history" not in section


def test_untracked_gets_no_estimate(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    assert "Netflix" in dashboard(path, NOV5)
    assert renewals(dashboard(path, NOV5)) == ""


def test_window_includes_day_seven_and_excludes_day_eight(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    assert "2026-11-12" in renewals(dashboard(path, date(2026, 11, 5)))
    assert "2026-11-12" in renewals(dashboard(path, date(2026, 11, 12)))
    assert renewals(dashboard(path, date(2026, 11, 4))) == ""


def ev(*dates):
    return tuple(dates)


def test_billing_day_clamps_to_short_months():
    nov30 = ev(date(2026, 11, 30), date(2026, 12, 31), date(2027, 1, 31))
    assert next_billing_date(date(2027, 1, 31), nov30) == date(2027, 2, 28)
    mid = ev(date(2027, 1, 30), date(2027, 2, 28), date(2027, 3, 30))
    assert next_billing_date(date(2027, 3, 30), mid) == date(2027, 4, 30)
    plain = ev(date(2026, 11, 12), date(2026, 12, 12))
    assert next_billing_date(date(2026, 12, 12), plain) == date(2027, 1, 12)
    leap = ev(date(2027, 12, 31), date(2028, 1, 31))
    assert next_billing_date(date(2028, 1, 31), leap) == date(2028, 2, 29)


def test_expected_date_passing_asks_for_history_and_does_not_roll_forward(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    for today in (date(2026, 11, 13), date(2026, 12, 5), date(2027, 3, 1)):
        section = renewals(dashboard(path, today))
        assert "2026-11-12" in section
        assert "import updated history" in section
        assert "2026-12-12" not in section
        assert section.count("2026-1") == 2 and "2027-" not in section


def test_a_posted_renewal_moves_the_estimate_to_next_month(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    assert "import updated history" in renewals(dashboard(path, date(2026, 11, 13)))
    load(env, "b.csv", [(d(11), "17.99")])
    assert renewals(dashboard(path, date(2026, 11, 13))) == ""
    section = renewals(dashboard(path, date(2026, 12, 8)))
    assert "2026-12-12" in section and "import updated history" not in section


def test_latest_import_date_is_per_account_and_shown(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    load(env, "b.csv", [(d(10, 29), "5.00")], account="other")
    track(path)
    assert "2026-10-12" in renewals(dashboard(path, NOV5))
    assert "2026-10-29" not in renewals(dashboard(path, NOV5))


def test_two_opens_on_the_same_day_agree(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    first, second = dashboard(path, NOV5), dashboard(path, NOV5)
    assert renewals(first) and first == second


def test_skipped_month_still_estimates_from_tracked_history(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    assert "import updated history" in renewals(dashboard(path, date(2026, 11, 13)))
    load(env, "b.csv", [(d(12), "20.99")])
    section = renewals(dashboard(path, date(2027, 1, 5)))
    assert "2027-01-12" in section and "$20.99" in section
    assert "import updated history" not in section
    assert "2026-12-12" in section


def test_pending_charge_never_advances_the_estimate(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    conn = store.open_db(str(path))
    try:
        conn.execute("UPDATE transactions SET pending = 1 WHERE date = '2026-10-12'")
        conn.commit()
    finally:
        conn.close()
    section = renewals(dashboard(path, date(2026, 10, 8)))
    assert "2026-10-12" in section and "$15.99" in section


def test_month_end_billing_day_matches_in_both_tables(env):
    _, path = env
    load(env, "a.csv", [("11/30/2026", "9.99"), ("12/31/2026", "9.99"),
                        ("01/31/2027", "9.99"), ("02/28/2027", "9.99")])
    track(path)
    out = dashboard(path, date(2027, 3, 29))
    table = out.split("Recurring Charges")[1].split("Upcoming Renewals")[0]
    assert "(tracked)" in table and " 31 " in table and " 28 " not in table
    assert "2027-03-31" in renewals(out)


def test_unrepresentable_year_skips_the_estimate_without_crashing(env):
    _, path = env
    rows = [("10/09/9999", "9.99"), ("11/09/9999", "9.99"), ("12/09/9999", "9.99")]
    load(env, "a.csv", rows)
    track(path)
    out = dashboard(path, date(9999, 12, 5))
    assert "Netflix" in out and renewals(out) == ""
    assert next_billing_date(date(9999, 12, 9), ev(date(9999, 12, 9))) is None


def test_same_day_charges_follow_first_observed_order_across_imports(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    load(env, "b.csv", [(d(10), "22.99")])
    load(env, "c.csv", [(d(10), "23.99")])
    assert "$23.99" in renewals(dashboard(path, NOV5))


def test_same_day_order_is_not_the_transaction_id_order(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    load(env, "b.csv", [(d(10), "23.99")])
    load(env, "c.csv", [(d(10), "22.99")])
    section = renewals(dashboard(path, NOV5))
    assert "$22.99" in section and "$23.99" not in section


def test_month_end_pattern_survives_a_short_posting_delay(env):
    _, path = env
    load(env, "a.csv", [("08/31/2025", "9.99"), ("09/30/2025", "9.99"),
                        ("10/31/2025", "9.99")])
    track(path)
    assert "2025-11-30" in renewals(dashboard(path, date(2025, 11, 25)))
    load(env, "b.csv", [("12/01/2025", "9.99")])
    out = dashboard(path, date(2025, 12, 25))
    assert "2025-12-31" in renewals(out) and "2026-01-01" not in renewals(out)
    assert "import updated history" not in renewals(out)
    assert query(path, "SELECT count(*) FROM transactions")[0][0] == 4


def test_a_short_delay_is_not_inferred_without_a_month_end_pattern(env):
    _, path = env
    load(env, "a.csv", EXAMPLE + [("12/01/2026", "17.99")])
    track(path)
    assert "2027-01-01" in renewals(dashboard(path, date(2026, 12, 28)))


def test_nothing_is_written_by_rendering(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    before = query(path, "SELECT count(*) FROM transactions")
    dashboard(path, NOV5)
    assert query(path, "SELECT count(*) FROM transactions") == before


def test_two_charges_on_one_delayed_posting_date_keep_the_cycle(env):
    _, path = env
    load(env, "a.csv", [("08/31/2025", "9.99"), ("09/30/2025", "9.99"),
                        ("10/31/2025", "9.99")])
    track(path)
    load(env, "b.csv", [("12/01/2025", "9.99")])
    load(env, "c.csv", [("12/01/2025", "10.99")])
    section = renewals(dashboard(path, date(2025, 12, 25)))
    assert "2025-12-31" in section and "$10.99" in section
    assert "2026-01-01" not in section and "$9.99" not in section


def test_dashboard_reads_one_snapshot_while_another_connection_imports(env, monkeypatch):
    import sqlite3
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    original = app.repository.load_transactions
    outcome = {}

    def load_then_import(conn, user_id):
        rows = original(conn, user_id)
        other = sqlite3.connect(str(path), timeout=0)
        try:
            cols = [r[1] for r in other.execute("PRAGMA table_info(transactions)")]
            pick = {"id": "'late'", "provider_id": "'late'", "date": "'2026-11-12'",
                    "amount_cents": "2099"}
            select = ", ".join(pick.get(c, c) for c in cols)
            other.execute(f"INSERT INTO transactions ({', '.join(cols)}) "
                          f"SELECT {select} FROM transactions WHERE date = '2026-10-12'")
            other.commit()
            outcome["imported"] = True
        except sqlite3.OperationalError:
            outcome["imported"] = False
        finally:
            other.close()
        return rows

    monkeypatch.setattr(app.repository, "load_transactions", load_then_import)
    section = renewals(dashboard(path, date(2026, 12, 10)))
    row = next(line for line in section.splitlines() if "Netflix" in line)
    coherent_before = ("2026-11-12" in row and "$17.99" in row and "2026-10-12" in row
                       and "import updated history" in row)
    coherent_after = ("2026-12-12" in row and "$20.99" in row and "2026-11-12" in row)
    assert coherent_before or coherent_after, row
    assert outcome["imported"] is False


def test_renewal_charges_are_scoped_to_the_source_owner(env):
    import sqlite3
    from core import users
    other = "22222222-2222-4222-8222-222222222222"
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    conn = store.open_db(str(path))
    try:
        users.create_user_with_hash(conn, "o@example.com",
                                    users.UNUSABLE_PASSWORD_HASH, user_id=other)
        provider = conn.execute("SELECT provider FROM transactions").fetchone()[0]
        conn.execute("INSERT INTO connections (provider, connection_id, user_id) "
                     "VALUES (?, 'theirs', ?)", (provider, other))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)")]
        pick = {"id": "'foreign'", "provider_id": "'foreign'",
                "connection_id": "'theirs'", "date": "'2026-10-20'",
                "amount_cents": "5999"}
        select = ", ".join(pick.get(c, c) for c in cols)
        conn.execute(f"INSERT INTO transactions ({', '.join(cols)}) "
                     f"SELECT {select} FROM transactions WHERE date = '2026-10-12'")
        conn.commit()
        mine = repository.load_tracked_charges(conn, app.users.LOCAL_USER_ID)
        theirs = repository.load_tracked_charges(conn, other)
    finally:
        conn.close()
    [history] = mine.values()
    assert [c for _, c in history] == [1599, 1599, 1799]
    assert theirs == {}
    section = renewals(dashboard(path, NOV5))
    assert "2026-11-12" in section and "$17.99" in section and "$59.99" not in section


def test_backfill_in_year_one_does_not_crash_cycle_inference(env):
    _, path = env
    load(env, "a.csv", EXAMPLE)
    track(path)
    load(env, "b.csv", [("01/01/0001", "9.99"), ("01/02/0001", "9.99"),
                        ("01/03/0001", "9.99")])
    out = dashboard(path, NOV5)
    assert "Netflix" in out and "$17.99" in out


def test_window_at_the_end_of_the_calendar_is_bounded():
    from insights import estimate_renewals
    histories = {("a", "M"): [(date(9999, 11, 30), 999), (date(9999, 12, 31), 999)]}
    assert estimate_renewals(histories, {}, date.max) == []
    histories = {("a", "M"): [(date(9999, 10, 31), 999), (date(9999, 11, 30), 999)]}
    [r] = estimate_renewals(histories, {}, date.max)
    assert r.expected == date(9999, 12, 31) and r.overdue is False
    [r] = estimate_renewals(histories, {}, date(9999, 12, 25))
    assert r.expected == date(9999, 12, 31)
    assert estimate_renewals(histories, {}, date(9999, 12, 23)) == []
    assert len(estimate_renewals(histories, {}, date(9999, 12, 24))) == 1
