from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

import import_csv

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "fixtures" / "bank_export.csv"
NETFLIX = (
    "Date,Description,Amount\n"
    "08/12/2026,NETFLIX.COM,-15.99\n"
    "09/12/2026,NETFLIX.COM,-15.99\n"
    "10/12/2026,NETFLIX.COM,-17.99\n"
)


@pytest.fixture
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "fresh.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("DATA_SOURCE", "fake")
    return path


def rows(db_file, sql):
    con = sqlite3.connect(db_file)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def counts(db_file):
    return dict(rows(
        db_file,
        "SELECT a.name, COUNT(t.id) FROM accounts a "
        "LEFT JOIN transactions t ON t.account_id = a.id GROUP BY a.name",
    ))


def test_two_accounts_hold_their_own_rows(db_file, tmp_path):
    netflix = tmp_path / "n.csv"
    netflix.write_text(NETFLIX)

    assert import_csv.main([str(SAMPLE), "--account", "A", "--yes"]) == 0
    assert import_csv.main([str(netflix), "--account", "B", "--yes"]) == 0

    assert counts(db_file) == {"A": 19, "B": 3}
    assert rows(db_file, "SELECT COUNT(DISTINCT user_id) FROM connections") == [(1,)]
    assert rows(db_file, "SELECT COUNT(*) FROM connections") == [(2,)]


def test_identical_rows_on_different_cards_get_distinct_ids(db_file, tmp_path):
    first = tmp_path / "one.csv"
    first.write_text(NETFLIX)

    assert import_csv.main([str(first), "--account", "A", "--yes"]) == 0
    assert import_csv.main([str(first), "--account", "B", "--yes"]) == 0

    ids = [r[0] for r in rows(db_file, "SELECT id FROM transactions")]
    assert len(ids) == 6
    assert len(set(ids)) == 6
    assert counts(db_file) == {"A": 3, "B": 3}


def test_renaming_the_file_does_not_change_the_destination(db_file, tmp_path):
    renamed = tmp_path / "completely-different-name.csv"
    shutil.copy(SAMPLE, renamed)

    assert import_csv.main([str(SAMPLE), "--account", "A", "--yes"]) == 0
    before = rows(db_file, "SELECT id, account_id FROM transactions ORDER BY id")
    assert len(before) == 19

    assert import_csv.main([str(renamed), "--account", "A", "--yes"]) == 0
    after = rows(db_file, "SELECT id, account_id FROM transactions ORDER BY id")

    assert after == before
    assert counts(db_file) == {"A": 19}


def test_only_the_requested_file_is_read(db_file, tmp_path, monkeypatch):
    decoy = tmp_path / "decoy.csv"
    decoy.write_text(NETFLIX)
    monkeypatch.setenv("CSV_PATH", str(decoy))
    monkeypatch.setenv("DATA_SOURCE", "plaid")

    assert import_csv.main([str(SAMPLE), "--account", "A", "--yes"]) == 0

    assert counts(db_file) == {"A": 19}
    assert rows(db_file, "SELECT COUNT(*) FROM connections") == [(1,)]


def test_blank_account_is_rejected_before_the_database_exists(db_file, capsys):
    assert import_csv.main([str(SAMPLE), "--account", "  ", "--yes"]) != 0
    assert "account identifier" in capsys.readouterr().err
    assert not db_file.exists()


def test_empty_file_argument_is_rejected_despite_a_valid_csv_path(
        db_file, tmp_path, monkeypatch, capsys):
    decoy = tmp_path / "decoy.csv"
    decoy.write_text(NETFLIX)
    monkeypatch.setenv("CSV_PATH", str(decoy))

    assert import_csv.main(["", "--account", "A", "--yes"]) != 0
    assert "file argument" in capsys.readouterr().err
    assert not db_file.exists()


def test_account_name_renders_as_literal_text(db_file):
    import io

    from rich.console import Console

    import main as dashboard

    names = ["[/bold]", "card:smile:"]
    for name in names:
        assert import_csv.main([str(SAMPLE), "--account", name, "--yes"]) == 0

    out = io.StringIO()
    dashboard.main(Console(file=out, width=120, color_system=None))

    rendered = out.getvalue()
    for name in names:
        assert name in rendered
    assert "\U0001F604" not in rendered

    assert counts(db_file) == {name: 19 for name in names}
    stored = [r[0] for r in rows(db_file, "SELECT id FROM accounts")]
    for name in names:
        assert any(s.endswith(":" + name.replace(":", "%3A")) for s in stored)


def test_script_entry_loads_dotenv_and_imports_do_not(tmp_path):
    import subprocess
    import sys

    project = tmp_path / "project"
    project.mkdir()
    for name in ("import_csv.py", "sync.py", "data_source.py", "insights.py",
                 "main.py"):
        shutil.copy(ROOT / name, project / name)
    for package in ("adapters", "core"):
        shutil.copytree(ROOT / package, project / package,
                        ignore=shutil.ignore_patterns("__pycache__"))

    configured = tmp_path / "configured.db"
    (project / ".env").write_text(f"DB_PATH={configured}\n")
    statement = tmp_path / "s.csv"
    statement.write_text(NETFLIX)

    env = {
        key: value for key, value in os.environ.items()
        if key not in ("DB_PATH", "DATA_SOURCE", "CSV_PATH")
        and not key.startswith("PLAID")
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    imported = subprocess.run(
        [sys.executable, "-c",
         "import import_csv, os; print(os.environ.get('DB_PATH'))"],
        cwd=project, env=env, capture_output=True, text=True,
    )
    assert imported.stdout.strip() == "None"

    result = subprocess.run(
        [sys.executable, str(project / "import_csv.py"), str(statement),
         "--account", "A", "--yes"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert counts(configured) == {"A": 3}
    assert list(project.rglob("*.db")) == []


def test_no_argument_workflow_still_syncs_the_configured_source(db_file):
    import sync

    runs = sync.sync()

    assert len(runs) == 1
    assert runs[0].connection.provider.value == "fake"
    assert rows(db_file, "SELECT COUNT(*) FROM transactions")[0][0] > 0


PREVIEW_CSV = (
    "\ufeffDate,Description,Amount\n"
    '01/05/2026,"ACME, INC ""STORE""","-$1,204.77"\n'
    "01/06/2026,REFUND ACME,25.00\n"
    "01/07/2026,COFFEE,USD -3.50\n"
)


def write(tmp_path, text, name="s.csv"):
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


def test_preview_then_cancel_leaves_fresh_path_absent(db_file, tmp_path, monkeypatch, capsys):
    path = write(tmp_path, PREVIEW_CSV)
    monkeypatch.setattr("builtins.input", lambda _="": "n")

    assert import_csv.main([str(path), "--account", "A"]) == 1

    out = capsys.readouterr().out
    assert "rows      3" in out
    assert "2026-01-05 to 2026-01-07" in out
    assert "charge" in out and "credit" in out
    assert "$1204.77" in out and 'ACME, INC "STORE"' in out
    assert "cancelled" in out
    assert not db_file.exists()


def test_eof_on_prompt_cancels(db_file, tmp_path, monkeypatch):
    path = write(tmp_path, PREVIEW_CSV)

    def eof(_=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert import_csv.main([str(path), "--account", "A"]) == 1
    assert not db_file.exists()


def test_confirm_imports_bom_quoted_refund(db_file, tmp_path, monkeypatch):
    path = write(tmp_path, PREVIEW_CSV)
    monkeypatch.setattr("builtins.input", lambda _="": "y")

    assert import_csv.main([str(path), "--account", "A"]) == 0

    stored = rows(db_file, "SELECT name, amount_cents FROM transactions ORDER BY date")
    assert stored == [('ACME, INC "STORE"', 120477), ("REFUND ACME", -2500),
                      ("COFFEE", 350)]


@pytest.mark.parametrize("bad, reason", [
    ("01/05/2026,OK,-1.00\n01/06/2026,BAD,abc\n", "cannot parse amount"),
    ("01/05/2026,OK,-1.00\n01/06/2026,TRUNCATED\n", "missing field"),
    ("01/05/2026,OK,-1.00\n01/06/2026,EURO,EUR -5.00\n", "unsupported currency"),
    ("01/05/2026,OK,-1.00\n01/06/2026,EURO,-\u20ac5.00\n", "unsupported currency"),
])
def test_bad_row_names_row_and_commits_nothing_fresh(db_file, tmp_path, bad, reason, capsys):
    path = write(tmp_path, "Date,Description,Amount\n" + bad)

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1

    err = capsys.readouterr().err
    assert reason in err and "line 3" in err
    assert "nothing was written" in err
    assert not db_file.exists()


def test_rejection_and_cancel_leave_existing_database_unchanged(db_file, tmp_path, monkeypatch):
    good = write(tmp_path, NETFLIX, "good.csv")
    assert import_csv.main([str(good), "--account", "A", "--yes"]) == 0
    sql = "SELECT id, account_id, date, amount_cents FROM transactions ORDER BY id"
    before = rows(db_file, sql)
    assert len(before) == 3  # positive control: there is something to compare

    bad = write(tmp_path, "Date,Description,Amount\n01/05/2026,X,-1.00\n01/06/2026,Y,EUR 2\n", "bad.csv")
    assert import_csv.main([str(bad), "--account", "A", "--yes"]) == 1
    assert rows(db_file, sql) == before

    other = write(tmp_path, PREVIEW_CSV, "other.csv")
    monkeypatch.setattr("builtins.input", lambda _="": "")
    assert import_csv.main([str(other), "--account", "B"]) == 1
    assert rows(db_file, sql) == before
    assert counts(db_file) == {"A": 3}


def test_saved_rows_are_the_previewed_snapshot_and_file_is_read_once(
        db_file, tmp_path, monkeypatch):
    path = write(tmp_path, NETFLIX)
    reads = []
    real = import_csv.read_rows

    def counting(p):
        reads.append(p)
        return real(p)

    monkeypatch.setattr(import_csv, "read_rows", counting)

    def change_then_confirm(_=""):
        path.write_text(
            "Date,Description,Amount\n01/01/2026,SUBSTITUTE,-999.00\n")
        return "y"

    monkeypatch.setattr("builtins.input", change_then_confirm)
    assert import_csv.main([str(path), "--account", "A"]) == 0

    stored = rows(db_file, "SELECT name, amount_cents FROM transactions ORDER BY date")
    assert len(stored) == 3
    assert [r[0] for r in stored] == ["NETFLIX.COM"] * 3
    assert [r[1] for r in stored] == [1599, 1599, 1799]
    assert len(reads) == 1


def test_confirmed_import_works_after_the_file_is_deleted(db_file, tmp_path, monkeypatch):
    path = write(tmp_path, NETFLIX)

    def delete_then_confirm(_=""):
        path.unlink()
        return "y"

    monkeypatch.setattr("builtins.input", delete_then_confirm)
    assert import_csv.main([str(path), "--account", "A"]) == 0
    assert counts(db_file) == {"A": 3}


def test_snapshot_keeps_ids_ownership_and_duplicate_occurrences(db_file, tmp_path):
    from adapters.csv_source import CONNECTION_ID, CsvAccount, CsvSource
    from core import store, users
    from core.models import scoped_connection_id

    twin = "Date,Description,Amount\n01/05/2026,TWIN,-5.00\n01/05/2026,TWIN,-5.00\n"
    path = write(tmp_path, twin)
    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 0

    stored = rows(db_file, "SELECT id, account_id FROM transactions ORDER BY id")
    assert len(stored) == 2
    assert len({r[0] for r in stored}) == 2

    conn = store.open_db()
    try:
        user_id = users.ensure_local_user(conn)
    finally:
        conn.close()
    direct = CsvSource(
        path=str(path),
        account=CsvAccount(provider_id="A", name="A"),
        connection_id=scoped_connection_id(f"{CONNECTION_ID}/A", user_id),
    )
    expected = sorted((t.id, t.account_id) for t in direct.fetch_transactions().added)
    assert sorted(stored) == expected
    assert rows(db_file, "SELECT COUNT(*) FROM accounts WHERE id = '%s'" % stored[0][1]) == [(1,)]


@pytest.mark.parametrize("amount, reason", [
    ("1USD2.34", "malformed amount syntax"),
    ('"-1,23"', "malformed amount syntax"),
    ("EUR -5.00", "unsupported currency"),
    ("-€5.00", "unsupported currency"),
])
def test_invalid_amount_on_last_row_commits_nothing(db_file, tmp_path, amount, reason, capsys):
    path = write(tmp_path, f"Date,Description,Amount\n01/05/2026,OK,-1.00\n01/06/2026,LAST,{amount}\n")

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1

    err = capsys.readouterr().err
    assert "line 3" in err and reason in err
    assert not db_file.exists()


@pytest.mark.parametrize("cell", [
    '"-$1,204.77"', "-$17.99", "-17.99", '"$2,300.00"', "USD -3.50", "-3.50 USD", "+5.00",
])
def test_valid_amount_forms_import(db_file, tmp_path, cell):
    path = write(tmp_path, f"Date,Description,Amount\n01/05/2026,OK,{cell}\n")
    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 0
    assert rows(db_file, "SELECT COUNT(*) FROM transactions") == [(1,)]


def test_malformed_quoting_is_rejected_with_its_line(db_file, tmp_path, capsys):
    path = write(tmp_path, 'Date,Description,Amount\n01/05/2026,OK,-1.00\n01/06/2026,"BROKEN"x,-2.00\n')

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1

    err = capsys.readouterr().err
    assert "line 3" in err and "malformed CSV" in err
    assert not db_file.exists()


def test_error_line_is_physical_after_multiline_field_and_blank_line(db_file, tmp_path, capsys):
    text = ('Date,Description,Amount\n'
            '01/05/2026,"LINE1\nLINE2",-1.00\n'
            '\n'
            '01/06/2026,BAD,abc\n')
    path = write(tmp_path, text)

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1

    assert "line 5" in capsys.readouterr().err
    assert not db_file.exists()


@pytest.mark.parametrize("bom", ["", "﻿"])
def test_multiline_escaped_quotes_with_and_without_bom(db_file, tmp_path, bom):
    text = bom + 'Date,Description,Amount\n01/05/2026,"SAY ""HI""\nTWICE",-1.00\n'
    path = write(tmp_path, text)

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 0

    assert rows(db_file, "SELECT name FROM transactions") == [('SAY "HI"\nTWICE',)]


def test_preview_escapes_terminal_controls_but_storage_is_unchanged(db_file, tmp_path, capsys):
    nasty = "X\x1b[2J\nfake line\raccount   EVIL\x07\u202E"
    path = write(tmp_path, 'Date,Description,Amount\n01/05/2026,"%s",-1.00\n' % nasty)

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 0

    out = capsys.readouterr().out
    assert r"\x1b[2J" in out and r"\x0a" in out and r"\u202e" in out
    for bad in ("\x1b", "\x07", "\r", "\u202E"):
        assert bad not in out
    assert [l for l in out.splitlines() if l.startswith("account")] == ["account   A"]
    assert rows(db_file, "SELECT name FROM transactions") == [(nasty,)]


def test_account_identifier_with_line_break_is_rejected_before_database(db_file, tmp_path, capsys):
    path = write(tmp_path, NETFLIX)

    assert import_csv.main([str(path), "--account", "A\naccount   EVIL", "--yes"]) == 1

    assert "control or line-break" in capsys.readouterr().err
    assert not db_file.exists()


def test_save_failure_message_does_not_claim_the_database_was_untouched(
        db_file, tmp_path, monkeypatch, capsys):
    path = write(tmp_path, NETFLIX)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(import_csv.sync, "sync_source", boom)

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1

    err = capsys.readouterr().err
    assert "while saving" in err and "disk full" in err
    assert "nothing was written" not in err
    assert rows(db_file, "SELECT COUNT(*) FROM transactions") == [(0,)]


def run_cli(tmp_path, path, db, encoding="cp1252", extra=()):
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items()
           if k not in ("DB_PATH", "DATA_SOURCE", "CSV_PATH")
           and not k.startswith("PLAID")}
    env["DB_PATH"] = str(db)
    env["PYTHONIOENCODING"] = encoding
    code = ("import sys, import_csv; "
            "sys.exit(import_csv.main(sys.argv[1:]))")
    return subprocess.run(
        [sys.executable, "-c", code, str(path), "--account", "A", "--yes", *extra],
        cwd=ROOT, env=env, capture_output=True,
    )


def test_cp1252_stream_gets_escaped_text_and_storage_keeps_the_original(tmp_path):
    path = write(tmp_path, "Date,Description,Amount\n01/05/2026,東京,-1.00\n")
    db = tmp_path / "cp.db"

    result = run_cli(tmp_path, path, db)

    assert result.returncode == 0, result.stderr
    assert rb"\u6771\u4eac" in result.stdout
    assert rows(db, "SELECT name FROM transactions") == [("東京",)]


def test_cp1252_error_output_is_escaped_too(tmp_path):
    path = write(tmp_path, "Date,Description,Amount\n01/05/2026,東,EUR 5\n")
    db = tmp_path / "cp2.db"

    result = run_cli(tmp_path, path, db)

    assert result.returncode == 1
    assert b"unsupported currency" in result.stderr
    assert not db.exists()


def test_currency_column_with_eur_is_rejected_with_its_row(db_file, tmp_path, capsys):
    path = write(tmp_path, "Date,Description,Amount,Currency\n"
                           "01/05/2026,OK,-1.00,USD\n"
                           "01/06/2026,EURO,-5.00,EUR\n")

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1

    err = capsys.readouterr().err
    assert "line 3" in err and "unsupported currency 'EUR'" in err
    assert not db_file.exists()


def test_currency_column_usd_and_blank_import(db_file, tmp_path):
    path = write(tmp_path, "Date,Description,Amount,Currency\n"
                           "01/05/2026,A,-1.00,usd\n01/06/2026,B,-2.00,\n")
    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 0
    assert rows(db_file, "SELECT amount_cents FROM transactions ORDER BY date") == [(100,), (200,)]


@pytest.mark.parametrize("header", [
    "Date,Description,Amount,Amount",
    "Date,Date,Description,Amount",
    "Date,Description,Amount,Currency,Currency",
])
def test_duplicate_headers_are_rejected(db_file, tmp_path, header, capsys):
    path = write(tmp_path, f"{header}\n01/05/2026,X,-1.00{',1' * (len(header.split(',')) - 3)}\n")

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1

    assert "duplicate column" in capsys.readouterr().err
    assert not db_file.exists()


@pytest.mark.parametrize("cell", ["92233720368547758.09", "-92233720368547758.08"])
def test_out_of_range_final_amount_is_rejected_before_database(db_file, tmp_path, cell, capsys):
    path = write(tmp_path, f"Date,Description,Amount\n01/05/2026,OK,-1.00\n01/06/2026,BIG,{cell}\n")

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1

    err = capsys.readouterr().err
    assert "line 3" in err and "outside the supported range" in err
    assert not db_file.exists()


@pytest.mark.parametrize("cell, cents", [
    ("-92233720368547758.07", 9223372036854775807),
    ("92233720368547758.08", -9223372036854775808),
    ("$-5.00", 500),
])
def test_boundary_and_dollar_before_minus_amounts_store_exact_cents(db_file, tmp_path, cell, cents):
    path = write(tmp_path, f"Date,Description,Amount\n01/05/2026,OK,{cell}\n")
    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 0
    assert rows(db_file, "SELECT amount_cents FROM transactions") == [(cents,)]


def test_unquoted_quote_is_rejected_but_escaped_quotes_survive(db_file, tmp_path, capsys):
    bad = write(tmp_path, 'Date,Description,Amount\n01/05/2026,OK,-1.00\n01/06/2026,BA"D,-2.00\n', "bad.csv")
    assert import_csv.main([str(bad), "--account", "A", "--yes"]) == 1
    err = capsys.readouterr().err
    assert "line 3" in err and "quote inside an unquoted field" in err
    assert not db_file.exists()

    good = write(tmp_path, 'Date,Description,Amount\n01/05/2026,"BA""D",-2.00\n', "good.csv")
    assert import_csv.main([str(good), "--account", "A", "--yes"]) == 0
    assert rows(db_file, "SELECT name FROM transactions") == [('BA"D',)]


def test_unterminated_multiline_record_reports_its_line_range(db_file, tmp_path, capsys):
    path = write(tmp_path, 'Date,Description,Amount\n\n01/05/2026,OK,-1.00\n01/06/2026,"OPEN\nstill open\n')

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1

    assert "line 4-6" in capsys.readouterr().err
    assert not db_file.exists()


def test_invalid_utf8_in_final_row_reports_line_and_reason(db_file, tmp_path, capsys):
    path = tmp_path / "bad.csv"
    path.write_bytes(b"Date,Description,Amount\n\n01/05/2026,OK,-1.00\n01/06/2026,BAD\xff,-2.00\n")

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1

    err = capsys.readouterr().err
    assert "line 4" in err and "invalid UTF-8" in err and "invalid start byte" in err
    assert not db_file.exists()


def test_invalid_input_leaves_existing_database_unchanged(db_file, tmp_path):
    good = write(tmp_path, NETFLIX, "good.csv")
    assert import_csv.main([str(good), "--account", "A", "--yes"]) == 0
    sql = "SELECT id, account_id, amount_cents FROM transactions ORDER BY id"
    before = rows(db_file, sql)
    assert len(before) == 3

    for name, body in {
        "eur.csv": "Date,Description,Amount,Currency\n01/05/2026,E,-5.00,EUR\n",
        "big.csv": "Date,Description,Amount\n01/05/2026,B,92233720368547758.09\n",
        "quote.csv": 'Date,Description,Amount\n01/05/2026,BA"D,-1.00\n',
    }.items():
        path = write(tmp_path, body, name)
        assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1
        assert rows(db_file, sql) == before


def test_fractional_cent_beyond_decimal_precision_is_rejected(db_file, tmp_path, capsys):
    path = write(tmp_path, "Date,Description,Amount\n01/05/2026,OK,-1.00\n"
                           "01/06/2026,TINY,1.000000000000000000000000000001\n")

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1

    err = capsys.readouterr().err
    assert "line 3" in err and "more precision" in err
    assert not db_file.exists()


@pytest.mark.parametrize("cell, cents", [
    ("-1.500", 150),
    ("-1.5000000000000000000000000000", 150),
    ("-92233720368547758.0700", 9223372036854775807),
])
def test_valid_trailing_zeros_still_import(db_file, tmp_path, cell, cents):
    path = write(tmp_path, f"Date,Description,Amount\n01/05/2026,OK,{cell}\n")
    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 0
    assert rows(db_file, "SELECT amount_cents FROM transactions") == [(cents,)]


@pytest.mark.parametrize("header", ["currency", "Currency ", " CURRENCY", "Currency"])
def test_currency_header_variants_still_reject_eur(db_file, tmp_path, header, capsys):
    path = write(tmp_path, f"Date,Description,Amount,{header}\n"
                           "01/05/2026,OK,-1.00,USD\n01/06/2026,EURO,-5.00,EUR\n")

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1

    err = capsys.readouterr().err
    assert "line 3" in err and "unsupported currency 'EUR'" in err
    assert not db_file.exists()


@pytest.mark.parametrize("extra", ["currency", "Currency ", " CURRENCY "])
def test_currency_headers_differing_by_case_or_space_are_duplicates(db_file, tmp_path, extra, capsys):
    path = write(tmp_path, f"Date,Description,Amount,Currency,{extra}\n"
                           "01/05/2026,OK,-1.00,USD,EUR\n")

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1

    assert "duplicate column" in capsys.readouterr().err
    assert not db_file.exists()


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_bad_byte_right_after_crlf_reports_physical_line_3(db_file, tmp_path, bom, capsys):
    path = tmp_path / "bom.csv"
    path.write_bytes(bom + b"Date,Description,Amount\r\n01/05/2026,OK,-1.00\r\n\xff1/06/2026,BAD,-2.00\r\n")

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1

    err = capsys.readouterr().err
    assert "line 3" in err and "invalid UTF-8" in err
    assert not db_file.exists()


def test_long_malformed_whitespace_is_rejected_promptly(db_file, tmp_path, capsys):
    import time

    cell = '"' + " " * 200_000 + '!"'
    path = write(tmp_path, f"Date,Description,Amount\n01/05/2026,OK,{cell}\n")

    started = time.monotonic()
    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1
    elapsed = time.monotonic() - started

    assert "line 2" in capsys.readouterr().err
    assert elapsed < 30
    assert not db_file.exists()


def test_underflowing_fractional_amount_is_rejected_with_its_row(db_file, tmp_path, capsys):
    tiny = "0." + "0" * 1_000_100 + "1"
    path = write(tmp_path, "Date,Description,Amount\n01/05/2026,OK,-1.00\n"
                           f"01/06/2026,TINY,{tiny}\n")

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1

    err = capsys.readouterr().err
    assert "line 3" in err and "more precision" in err
    assert not db_file.exists()


def test_oversized_whole_amount_is_rejected_with_its_row_before_database(db_file, tmp_path, capsys):
    huge = "1" * 4301
    path = write(tmp_path, "Date,Description,Amount\n01/05/2026,OK,-1.00\n"
                           f"01/06/2026,HUGE,{huge}\n")

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 1

    err = capsys.readouterr().err
    assert "line 3" in err and "outside the supported range" in err
    assert not db_file.exists()


def summary(capsys):
    out = capsys.readouterr().out
    new = [l for l in out.splitlines() if l.startswith("  new")]
    stored = [l for l in out.splitlines() if l.startswith("  stored")]
    return new, stored


def test_first_and_repeat_import_report_new_and_stored_counts(db_file, capsys):
    assert import_csv.main([str(SAMPLE), "--account", "A", "--yes"]) == 0
    new, stored = summary(capsys)
    assert new == ["  new       19 transactions"]
    assert stored[0].startswith("  stored    0 ")
    sql = ("SELECT COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents END), 0), "
           "COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents END), 0) "
           "FROM transactions")
    assert rows(db_file, sql) == [(257334, 230000)]
    before = rows(db_file, "SELECT id FROM transactions ORDER BY id")
    assert len(before) == 19

    assert import_csv.main([str(SAMPLE), "--account", "A", "--yes"]) == 0
    new, stored = summary(capsys)
    assert new == ["  new       0 transactions"]
    assert stored[0].startswith("  stored    19 ")
    assert rows(db_file, "SELECT id FROM transactions ORDER BY id") == before


def test_overlapping_statement_adds_only_the_new_charge(db_file, tmp_path, capsys, monkeypatch):
    for key in [k for k in os.environ if k.startswith("PLAID")]:
        monkeypatch.delenv(key)
    assert import_csv.main([str(SAMPLE), "--account", "A", "--yes"]) == 0
    capsys.readouterr()
    sql = "SELECT id, name, amount_cents FROM transactions ORDER BY id"
    before = rows(db_file, sql)
    assert len(before) == 19

    lines = SAMPLE.read_text(encoding="utf-8-sig").splitlines()
    omitted = lines[1]
    assert "SAFEWAY" in omitted
    partial = tmp_path / "partial.csv"
    body = "\n".join(lines[:1] + lines[2:])
    partial.write_text(body + "\n12/31/2026,UNIQUE NEW MERCHANT,-7.77\n",
                       encoding="utf-8")
    assert import_csv.main([str(partial), "--account", "A", "--yes"]) == 0
    new, stored = summary(capsys)

    assert new == ["  new       1 transactions"]
    assert stored[0].startswith("  stored    18 ")
    after = rows(db_file, sql)
    assert len(after) == 20
    assert set(before) < set(after)
    added = set(after) - set(before)
    assert [(n, c) for _, n, c in added] == [("UNIQUE NEW MERCHANT", 777)]
    assert any("SAFEWAY" in n for _, n, _ in after)


def test_same_sample_in_second_account_is_all_new_with_disjoint_ids(
        db_file, capsys, monkeypatch):
    for key in [k for k in os.environ if k.startswith("PLAID")]:
        monkeypatch.delenv(key)
    assert import_csv.main([str(SAMPLE), "--account", "A", "--yes"]) == 0
    capsys.readouterr()
    assert import_csv.main([str(SAMPLE), "--account", "B", "--yes"]) == 0
    new, stored = summary(capsys)

    assert new == ["  new       19 transactions"]
    assert stored[0].startswith("  stored    0 ")
    assert counts(db_file) == {"A": 19, "B": 19}
    ids = {}
    for name in ("A", "B"):
        ids[name] = {r[0] for r in rows(
            db_file,
            "SELECT t.id FROM transactions t JOIN accounts a "
            f"ON a.id = t.account_id WHERE a.name = '{name}'")}
        assert len(ids[name]) == 19
    assert ids["A"].isdisjoint(ids["B"])


def test_identical_same_day_rows_count_as_two_new_then_two_stored(db_file, tmp_path, capsys):
    path = write(tmp_path, "Date,Description,Amount\n01/05/2026,TWIN,-5.00\n01/05/2026,TWIN,-5.00\n")
    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 0
    new, stored = summary(capsys)
    assert new == ["  new       2 transactions"] and stored[0].startswith("  stored    0 ")
    assert rows(db_file, "SELECT COUNT(*) FROM transactions") == [(2,)]

    assert import_csv.main([str(path), "--account", "A", "--yes"]) == 0
    new, stored = summary(capsys)
    assert new == ["  new       0 transactions"] and stored[0].startswith("  stored    2 ")
    assert rows(db_file, "SELECT COUNT(*) FROM transactions") == [(2,)]
