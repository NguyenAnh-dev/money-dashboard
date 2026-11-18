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

    assert import_csv.main([str(SAMPLE), "--account", "A"]) == 0
    assert import_csv.main([str(netflix), "--account", "B"]) == 0

    assert counts(db_file) == {"A": 19, "B": 3}
    assert rows(db_file, "SELECT COUNT(DISTINCT user_id) FROM connections") == [(1,)]
    assert rows(db_file, "SELECT COUNT(*) FROM connections") == [(2,)]


def test_identical_rows_on_different_cards_get_distinct_ids(db_file, tmp_path):
    first = tmp_path / "one.csv"
    first.write_text(NETFLIX)

    assert import_csv.main([str(first), "--account", "A"]) == 0
    assert import_csv.main([str(first), "--account", "B"]) == 0

    ids = [r[0] for r in rows(db_file, "SELECT id FROM transactions")]
    assert len(ids) == 6
    assert len(set(ids)) == 6
    assert counts(db_file) == {"A": 3, "B": 3}


def test_renaming_the_file_does_not_change_the_destination(db_file, tmp_path):
    renamed = tmp_path / "completely-different-name.csv"
    shutil.copy(SAMPLE, renamed)

    assert import_csv.main([str(SAMPLE), "--account", "A"]) == 0
    before = rows(db_file, "SELECT id, account_id FROM transactions ORDER BY id")
    assert len(before) == 19

    assert import_csv.main([str(renamed), "--account", "A"]) == 0
    after = rows(db_file, "SELECT id, account_id FROM transactions ORDER BY id")

    assert after == before
    assert counts(db_file) == {"A": 19}


def test_only_the_requested_file_is_read(db_file, tmp_path, monkeypatch):
    decoy = tmp_path / "decoy.csv"
    decoy.write_text(NETFLIX)
    monkeypatch.setenv("CSV_PATH", str(decoy))
    monkeypatch.setenv("DATA_SOURCE", "plaid")

    assert import_csv.main([str(SAMPLE), "--account", "A"]) == 0

    assert counts(db_file) == {"A": 19}
    assert rows(db_file, "SELECT COUNT(*) FROM connections") == [(1,)]


def test_blank_account_is_rejected_before_the_database_exists(db_file, capsys):
    assert import_csv.main([str(SAMPLE), "--account", "  "]) != 0
    assert "account identifier" in capsys.readouterr().err
    assert not db_file.exists()


def test_empty_file_argument_is_rejected_despite_a_valid_csv_path(
        db_file, tmp_path, monkeypatch, capsys):
    decoy = tmp_path / "decoy.csv"
    decoy.write_text(NETFLIX)
    monkeypatch.setenv("CSV_PATH", str(decoy))

    assert import_csv.main(["", "--account", "A"]) != 0
    assert "file argument" in capsys.readouterr().err
    assert not db_file.exists()


def test_account_name_renders_as_literal_text(db_file):
    import io

    from rich.console import Console

    import main as dashboard

    names = ["[/bold]", "card:smile:"]
    for name in names:
        assert import_csv.main([str(SAMPLE), "--account", name]) == 0

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
         "--account", "A"],
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
