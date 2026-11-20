import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
STATEMENT = (
    "Date,Description,Amount\n"
    "08/12/2026,NETFLIX.COM,-15.99\n"
    "09/12/2026,NETFLIX.COM,-15.99\n"
    "10/12/2026,NETFLIX.COM,-17.99\n"
)


def test_readme_links_and_scripts_exist():
    links = re.findall(r"\]\(([^)#]+)\)", README)
    scripts = re.findall(r"python (\S+\.py)", README)
    assert links and scripts
    for target in links + scripts:
        if target in ("netflix.csv",):
            continue
        assert (ROOT / target).exists(), target


def test_readme_statement_matches_the_test_fixture():
    assert STATEMENT.replace("\n", "\n   ").strip() in README.replace(
        "\r\n", "\n")


def run(tmp_path, *args):
    env = {**os.environ, "DB_PATH": str(tmp_path / "demo.db"),
           "PYTHONIOENCODING": "utf-8"}
    env.pop("DATA_SOURCE", None)
    return subprocess.run([sys.executable, *args], cwd=tmp_path, env=env,
                          capture_output=True, text=True,
                          encoding="utf-8", timeout=120)


def script(name):
    return str(ROOT / name)


def commands(start, end):
    section = README[README.index(start):README.index(end)]
    return [line.strip().split() for line in section.splitlines()
            if line.strip().startswith("python ")]


def walkthrough():
    return commands("## Try it", "8. **Reopen from a fresh terminal.**")


def reopen():
    return commands("8. **Reopen from a fresh terminal.**",
                    "## Optional: dismiss and restore")


def test_readme_reopen_step_names_the_setup():
    step = README[README.index("8. **Reopen"):README.index("## Optional")]
    for needle in ("repository root", "Activate the virtual environment",
                   "same disposable file", "no price increases",
                   "no Price Increases table"):
        assert needle in step
    assert reopen() == [["python", "subscriptions.py", "warnings"],
                        ["python", "main.py"]]


def execute(tmp_path, words, stdin=None):
    assert words[0] == "python" and words[1].endswith(".py"), words
    env = {**os.environ, "DB_PATH": str(tmp_path / "demo.db"),
           "PYTHONIOENCODING": "utf-8"}
    for key in list(env):
        if key.startswith(("PLAID", "DATA_SOURCE")):
            del env[key]
    return subprocess.run([sys.executable, str(ROOT / words[1]), *words[2:]],
                          cwd=tmp_path, env=env, input=stdin or "",
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=120)


def test_main_walkthrough_has_no_dismiss_or_restore():
    steps = [" ".join(w) for w in walkthrough()]
    assert any("subscriptions.py track" in s for s in steps)
    assert not [s for s in steps if " dismiss " in s or " restore " in s]
    assert README.index("subscriptions.py dismiss") > README.index(
        "subscriptions.py acknowledge")


def test_readme_documents_each_shell():
    for needle in ('$env:DB_PATH = "demo.db"', 'set "DB_PATH=demo.db"',
                   "export DB_PATH=demo.db", "Activate.ps1", "activate.bat",
                   "source venv/bin/activate"):
        assert needle in README


def test_documented_walkthrough_ends_acknowledged_and_stays_cleared(tmp_path):
    (tmp_path / "netflix.csv").write_text(STATEMENT, encoding="utf-8")
    account = token = None
    seen = []
    for words in walkthrough():
        words = [account if w == "ACCOUNT_ID" else token if w == "TOKEN" else w
                 for w in words]
        assert None not in words, words
        result = execute(tmp_path, words, stdin="n\n")
        action = words[1:3]
        seen.append(action)
        if action[0] == "import_csv.py" and "--yes" not in words:
            assert "rows      3" in result.stdout
            assert not (tmp_path / "demo.db").exists()
            continue
        assert result.returncode == 0, (words, result.stderr)
        if action == ["subscriptions.py", "list"]:
            found = re.search(r"\[id: (.+)\]", result.stdout)
            assert found
            account = found.group(1)
        elif action == ["subscriptions.py", "warnings"]:
            found = re.search(r"\[event: (.+)\]", result.stdout)
            assert found and "$15.99 -> $17.99" in result.stdout
            token = found.group(1)
        elif action == ["main.py"]:
            assert "Price Increases" in result.stdout
    assert ["subscriptions.py", "acknowledge"] in seen

    for words in reopen() + [["python", "subscriptions.py", "list"]]:
        fresh = execute(tmp_path, words)
        assert fresh.returncode == 0, (words, fresh.stderr)
        assert "Price Increases" not in fresh.stdout
        assert "[event:" not in fresh.stdout
    assert "no price increases" in execute(
        tmp_path, ["python", "subscriptions.py", "warnings"]).stdout
    dash = execute(tmp_path, ["python", "main.py"]).stdout
    assert "MONEY DASHBOARD" in dash and "Netflix" in dash
    assert "(tracked)" in dash


def test_putting_restore_between_track_and_warnings_loses_the_warning(tmp_path):
    (tmp_path / "netflix.csv").write_text(STATEMENT, encoding="utf-8")
    execute(tmp_path, ["python", "import_csv.py", "netflix.csv", "--account",
                       "netflix-card", "--yes"])
    listing = execute(tmp_path, ["python", "subscriptions.py", "list"]).stdout
    account = re.search(r"\[id: (.+)\]", listing).group(1)
    base = ["python", "subscriptions.py"]
    tail = ["--account", account, "--merchant", "Netflix"]
    execute(tmp_path, base + ["track"] + tail)
    assert "[event:" in execute(tmp_path, base + ["warnings"]).stdout
    execute(tmp_path, base + ["dismiss"] + tail)
    execute(tmp_path, base + ["restore"] + tail)
    assert "no price increases" in execute(tmp_path, base + ["warnings"]).stdout
