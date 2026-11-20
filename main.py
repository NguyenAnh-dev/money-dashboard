import os
from datetime import date

from rich.console import Console
from rich.table import Table
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from core import repository, store, users
from core.money import format_money as dollars
from import_csv import safe
from insights import (total_balance, spending_by_category, find_recurring,
                      estimate_renewals, latest_import_dates, anchor_day)

# NOTE: there is deliberately no module-level `console`. The rendering target is
# threaded through as an argument so tests/check_baseline.py can pin width,
# encoding, and colour by PASSING a console rather than reassigning a global.
# Monkeypatching a module attribute did work -- globals are resolved at call
# time -- but it couples the gate to this file's internals: any show_* that
# later built its own Console would escape all three pins.


def balance_known(account):
    return account.balance_known


def show_accounts(accounts, console):
    names = account_labels(accounts)
    table = Table(title="Accounts", title_style="bold", header_style="bold cyan")
    table.add_column("Institution")
    table.add_column("Account")
    table.add_column("Balance", justify="right")

    for a in accounts:
        if balance_known(a):
            style = "red" if a.balance_cents < 0 else "green"
            balance = f"[{style}]{dollars(a.balance_cents)}[/{style}]"
        else:
            balance = "unknown"
        table.add_row(escape(a.institution), Text(safe(a.name)), balance)

    console.print(table)

    known = [a for a in accounts if balance_known(a)]
    unknown = [a for a in accounts if not balance_known(a)]
    net = sum(a.balance_cents for a in known)
    style = "red" if net < 0 else "green"
    if not unknown:
        console.print(Panel(f"[{style} bold]{dollars(net)}[/{style} bold]",
                            title="Net Worth (across all banks)", expand=False))
        return

    body = Text()
    if known:
        title = "Net Worth (known balances only)"
        body.append(dollars(net), style=f"{style} bold")
        body.append("\nIncluded: " + ", ".join(safe(names[a.id]) for a in known))
    else:
        title = "Net Worth"
        body.append("unavailable", style="bold")
        body.append("\nNo account has a known balance.")
    body.append("\nExcluded (balance unknown): "
                + ", ".join(safe(names[a.id]) for a in unknown))
    console.print(Panel(body, title=title, expand=False))


def show_recent_transactions(transactions, console, limit=10):
    table = Table(title=f"Recent Transactions (latest {limit})",
                  title_style="bold", header_style="bold cyan")
    table.add_column("Date")
    table.add_column("Merchant")
    table.add_column("Category")
    table.add_column("Type")
    table.add_column("Amount", justify="right")

    recent = sorted(transactions, key=lambda x: x.date, reverse=True)[:limit]
    for x in recent:
        if x.amount_cents < 0:
            kind = "Credit"
            amount = f"[green]{dollars(-x.amount_cents)}[/green]"
        else:
            kind = "Charge"
            amount = dollars(x.amount_cents)
        table.add_row(str(x.date), x.merchant, x.category, kind, amount)

    console.print(table)


def show_spending(transactions, console):
    table = Table(title="Spending by Category", title_style="bold",
                  header_style="bold cyan")
    table.add_column("Category")
    table.add_column("Amount", justify="right")

    for category, cents in spending_by_category(transactions):
        table.add_row(category, dollars(cents))

    console.print(table)


def account_labels(accounts):
    accounts = sorted(accounts, key=lambda a: a.id)
    shared = {}
    for a in accounts:
        name = a.name.rstrip()
        shared[name] = shared.get(name, 0) + 1

    first = {}
    for a in accounts:
        if shared[a.name.rstrip()] > 1 and a.mask:
            first[a.id] = f"{a.name.rstrip()} ({a.mask})"
        else:
            first[a.id] = a.name.rstrip()

    seen = {}
    for label in first.values():
        seen[label] = seen.get(label, 0) + 1

    labels = {}
    for a in accounts:
        if seen[first[a.id]] == 1:
            labels[a.id] = first[a.id]

    used = set(labels.values())
    for a in accounts:
        if a.id in labels:
            continue
        label = f"{first[a.id]} [id: {a.id}]"
        while label in used:
            label += "'"
        used.add(label)
        labels[a.id] = label
    return labels


def show_subscriptions(transactions, console, accounts=(), decisions=None):
    decisions = decisions or {}
    names = account_labels(accounts)
    table = Table(title="Recurring Charges", title_style="bold",
                  header_style="bold cyan")
    table.add_column("Merchant")
    table.add_column("Account")
    table.add_column("Amount", justify="right")
    table.add_column("Billing day", justify="right")
    table.add_column("Change", justify="right", no_wrap=True)
    table.add_column("Charges", no_wrap=True)

    shown = [(decisions.get((c.account_id, c.merchant)), c)
             for c in find_recurring(transactions)]
    shown = [(status, c) for status, c in shown if status != "dismissed"]
    shown.sort(key=lambda pair: pair[0] != "tracked")

    for status, c in shown:
        # direction is None when the price has never moved.
        if c.direction == "up":
            change = f"[red]+{dollars(c.delta_cents)}[/red]"
        elif c.direction == "down":
            change = f"[green]{dollars(c.delta_cents)}[/green]"
        else:
            change = ""

        history = Text("\n".join(f"{d} {dollars(cents)}"
                                 for d, cents in c.evidence))
        label = f"{c.merchant} (tracked)" if status == "tracked" else c.merchant
        day = (anchor_day(d for d, _ in c.evidence)
               if status == "tracked" else c.day)
        table.add_row(Text(label), Text(names.get(c.account_id, c.account_id)),
                      dollars(c.latest_cents), str(day), change, history)

    if shown:
        console.print(table)
    else:
        console.print("[bold]Recurring Charges[/bold]")
        console.print("  None yet. A monthly subscription needs charges in at "
                      "least 3 different months")
        console.print("  on the same account, about a month apart. Import more "
                      "history with:")
        console.print("      python import_csv.py FILE --account ACCOUNT_ID")


def show_price_increases(warnings, console, accounts=()):
    if not warnings:
        return
    names = account_labels(accounts)
    table = Table(title="Price Increases", title_style="bold",
                  header_style="bold cyan")
    for col in ("Merchant", "Account", "Date"):
        table.add_column(col)
    for col in ("Old", "New", "Increase"):
        table.add_column(col, justify="right")
    for w in warnings:
        table.add_row(Text(safe(w.merchant)),
                      Text(safe(names.get(w.account_id, w.account_id))),
                      str(w.date), dollars(w.old_cents), dollars(w.new_cents),
                      f"[red]+{dollars(w.delta_cents)}[/red]")
    console.print(table)


def show_renewals(renewals, console, accounts=()):
    if not renewals:
        return
    names = account_labels(accounts)
    table = Table(title="Upcoming Renewals (estimated)", title_style="bold",
                  header_style="bold cyan")
    for col in ("Merchant", "Account", "Expected"):
        table.add_column(col)
    table.add_column("Amount", justify="right")
    table.add_column("Latest import")
    table.add_column("Status")
    for r in renewals:
        status = ("[yellow]no charge seen - import updated history[/yellow]"
                  if r.overdue else "")
        table.add_row(Text(safe(r.merchant)),
                      Text(safe(names.get(r.account_id, r.account_id))),
                      str(r.expected), dollars(r.cents),
                      str(r.latest_import) if r.latest_import else "none",
                      status)
    console.print(table)


def show_no_data(console, path):
    console.print()
    console.print("[bold]  MONEY DASHBOARD[/bold]  [dim]v0.1[/dim]")
    console.print()
    console.print("  [yellow]No data yet.[/yellow] This screen reads from the "
                  "database, and nothing has been")
    console.print("  imported or synced into it. To import a bank statement CSV:")
    console.print()
    console.print("      [bold]python import_csv.py FILE --account ACCOUNT_ID[/bold]")
    console.print()
    console.print("  or, to fetch from a connected provider:")
    console.print()
    console.print("      [bold]python sync.py[/bold]")
    console.print()
    console.print(f"  [dim]looked in {path}[/dim]")
    console.print()


def source_label(providers):
    return ", ".join(providers)


def format_synced_at(stamp):
    if stamp is None:
        return "never"
    return stamp.replace("T", " ")[:16]


def main(console=None, today=None):
    """Render the dashboard.

    `console` is injected by the baseline gate. Left as None it builds one that
    adapts to the real terminal, which is what a human wants.
    """
    console = console or Console()
    today = today or date.today()

    path = store.db_path()

    if not os.path.exists(path):
        show_no_data(console, path)
        return

    user_id = users.LOCAL_USER_ID

    conn = store.open_db(path)
    try:
        conn.execute("BEGIN")
        accounts = repository.load_accounts(conn, user_id)
        transactions = repository.load_transactions(conn, user_id)
        synced_at = repository.last_synced_at(conn)
        providers = repository.stored_providers(conn, user_id)
        decisions = repository.load_decisions(conn, user_id)
        increases = repository.load_price_increases(conn, user_id)
        tracked = repository.load_tracked_charges(conn, user_id)
    finally:
        conn.close()

    if not accounts:
        show_no_data(console, path)
        return

    console.print()
    console.print(f"[bold]  MONEY DASHBOARD[/bold]  "
                  f"[dim]v0.1 - {source_label(providers)} data[/dim]")
    console.print()

    renewals = estimate_renewals(tracked, latest_import_dates(transactions),
                                 today)
    if not increases and not renewals:
        console.print("[bold]Alerts[/bold]")
        console.print("  No price increases or upcoming renewals. Tracked "
                      "subscriptions appear here.")
        console.print()
    if increases:
        show_price_increases(increases, console, accounts)
        console.print()
    if renewals:
        show_renewals(renewals, console, accounts)
        console.print()
    show_subscriptions(transactions, console, accounts, decisions)
    console.print()
    show_accounts(accounts, console)
    console.print()
    show_spending(transactions, console)
    console.print()
    show_recent_transactions(transactions, console)
    console.print()
    console.print(f"[dim]{len(transactions)} transactions across "
                  f"{len(accounts)} accounts at "
                  f"{len({a.institution for a in accounts})} institutions[/dim]")
    console.print(f"[dim]last synced {format_synced_at(synced_at)}[/dim]")
    console.print()


if __name__ == "__main__":
    # .env is loaded HERE, in the __main__ block, and nowhere else.
    #
    # Not at import: `tests/check_baseline.py` imports this module and calls
    # main(console) directly, and a module-level load_dotenv() would let a
    # stray DATA_SOURCE=plaid in someone's .env reach the gate. Guarding it
    # this way means the gate cannot load .env at all, rather than relying on
    # load_dotenv's no-override behaviour to save it. (It does not override --
    # the gate pins DATA_SOURCE before importing anything -- but two defences
    # cost one line, and the render being deterministic is the whole point.)
    #
    # Not in core/ or adapters/ either: reading process-wide config is an
    # entry-point job. An adapter that loads .env on import is an adapter you
    # cannot instantiate twice with different config.
    from dotenv import load_dotenv
    load_dotenv()

    main()
