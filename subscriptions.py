from __future__ import annotations

import argparse
import sys

from core import repository, store, users
from core.money import format_money
from import_csv import safe
from insights import find_recurring
from main import account_labels

STATUS_LABEL = {repository.TRACKED: "tracked", repository.DISMISSED: "dismissed"}


def candidates(conn, user_id):
    return find_recurring(repository.load_transactions(conn, user_id))


def list_lines(conn, user_id) -> list[str]:
    accounts = repository.load_accounts(conn, user_id)
    names = account_labels(accounts)
    decisions = repository.load_decisions(conn, user_id)
    found = {(c.account_id, c.merchant): c for c in candidates(conn, user_id)}
    lines = []
    for key in sorted(set(found) | set(decisions)):
        account_id, merchant = key
        status = STATUS_LABEL.get(decisions.get(key), "candidate")
        charge = found.get(key)
        amount = format_money(charge.latest_cents) if charge else "no longer detected"
        lines.append(
            f"{status:<9}  {safe(merchant)}  {amount}  "
            f"account {safe(names.get(account_id, account_id))}  [id: {safe(account_id)}]"
        )
    return lines


def decide(conn, user_id, action: str, account_id: str, merchant: str) -> str:
    if action == "restore":
        if not repository.clear_decision(conn, user_id, account_id, merchant):
            raise LookupError(f"no decision stored for {merchant!r} on {account_id!r}")
        return "restored"

    if not repository.owns_account(conn, user_id, account_id):
        raise LookupError(f"no such account {account_id!r}")
    if not any(c.account_id == account_id and c.merchant == merchant
               for c in candidates(conn, user_id)):
        raise LookupError(
            f"{merchant!r} is not a current candidate on account {account_id!r}"
        )
    try:
        repository.set_decision(
            conn, user_id, account_id, merchant,
            repository.TRACKED if action == "track" else repository.DISMISSED,
        )
    except repository.DecisionDismissed:
        raise LookupError(
            f"{merchant!r} on account {account_id!r} is dismissed; "
            f"run restore first, then track"
        ) from None
    return "tracked" if action == "track" else "dismissed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Review and decide on recurring-charge candidates.")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="show candidates and decisions")
    for name in ("track", "dismiss", "restore"):
        p = sub.add_parser(name)
        p.add_argument("--account", required=True, help="stored account id")
        p.add_argument("--merchant", required=True)
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass

    conn = store.open_db()
    try:
        user_id = users.ensure_local_user(conn)
        if args.action == "list":
            lines = list_lines(conn, user_id)
            print("\n".join(lines) if lines else "no candidates")
            return 0
        try:
            outcome = decide(conn, user_id, args.action, args.account, args.merchant)
        except LookupError as exc:
            print(f"{args.action} FAILED: {safe(exc)}", file=sys.stderr)
            return 1
        print(f"{outcome}: {safe(args.merchant)} on {safe(args.account)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    raise SystemExit(main())
