import os

from rich.console import Console
from rich.table import Table
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from core import repository, store, users
from core.money import format_money as dollars
from insights import total_balance, spending_by_category, find_recurring

# NOTE: there is deliberately no module-level `console`. The rendering target is
# threaded through as an argument so tests/check_baseline.py can pin width,
# encoding, and colour by PASSING a console rather than reassigning a global.
# Monkeypatching a module attribute did work -- globals are resolved at call
# time -- but it couples the gate to this file's internals: any show_* that
# later built its own Console would escape all three pins.


def show_accounts(accounts, console):
    table = Table(title="Accounts", title_style="bold", header_style="bold cyan")
    table.add_column("Institution")
    table.add_column("Account")
    table.add_column("Balance", justify="right")

    for a in accounts:
        style = "red" if a.balance_cents < 0 else "green"
        table.add_row(escape(a.institution), Text(a.name),
                      f"[{style}]{dollars(a.balance_cents)}[/{style}]")

    console.print(table)

    net = sum(a.balance_cents for a in accounts)
    style = "red" if net < 0 else "green"
    console.print(Panel(f"[{style} bold]{dollars(net)}[/{style} bold]",
                        title="Net Worth (across all banks)", expand=False))


def show_recent_transactions(transactions, console, limit=10):
    table = Table(title=f"Recent Transactions (latest {limit})",
                  title_style="bold", header_style="bold cyan")
    table.add_column("Date")
    table.add_column("Merchant")
    table.add_column("Category")
    table.add_column("Amount", justify="right")

    recent = sorted(transactions, key=lambda x: x.date, reverse=True)[:limit]
    for x in recent:
        if x.amount_cents < 0:
            amount = f"[green]{dollars(-x.amount_cents)}[/green]"
        else:
            amount = dollars(x.amount_cents)
        table.add_row(str(x.date), x.merchant, x.category, amount)

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


def show_subscriptions(transactions, console, accounts=()):
    names = account_labels(accounts)
    table = Table(title="Recurring Charges", title_style="bold",
                  header_style="bold cyan")
    table.add_column("Merchant")
    table.add_column("Account")
    table.add_column("Amount", justify="right")
    table.add_column("Billing day", justify="right")
    table.add_column("Change", justify="right", no_wrap=True)
    table.add_column("Charges", no_wrap=True)

    for c in find_recurring(transactions):
        # direction is None when the price has never moved.
        if c.direction == "up":
            change = f"[red]+{dollars(c.delta_cents)}[/red]"
        elif c.direction == "down":
            change = f"[green]{dollars(c.delta_cents)}[/green]"
        else:
            change = ""

        history = Text("\n".join(f"{d} {dollars(cents)}"
                                 for d, cents in c.evidence))
        table.add_row(Text(c.merchant), Text(names.get(c.account_id, c.account_id)),
                      dollars(c.latest_cents), str(c.day),
                      change, history)

    console.print(table)


def show_no_data(console, path):
    console.print()
    console.print("[bold]  MONEY DASHBOARD[/bold]  [dim]v0.1[/dim]")
    console.print()
    console.print("  [yellow]No data yet.[/yellow] This screen reads from the "
                  "database, and nothing has been")
    console.print("  synced into it. To fetch and store your data:")
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


def main(console=None):
    """Render the dashboard.

    `console` is injected by the baseline gate. Left as None it builds one that
    adapts to the real terminal, which is what a human wants.
    """
    console = console or Console()

    path = store.db_path()

    if not os.path.exists(path):
        show_no_data(console, path)
        return

    user_id = users.LOCAL_USER_ID

    conn = store.open_db(path)
    try:
        accounts = repository.load_accounts(conn, user_id)
        transactions = repository.load_transactions(conn, user_id)
        synced_at = repository.last_synced_at(conn)
        providers = repository.stored_providers(conn, user_id)
    finally:
        conn.close()

    if not accounts:
        show_no_data(console, path)
        return

    console.print()
    console.print(f"[bold]  MONEY DASHBOARD[/bold]  "
                  f"[dim]v0.1 - {source_label(providers)} data[/dim]")
    console.print()

    show_accounts(accounts, console)
    console.print()
    show_subscriptions(transactions, console, accounts)
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
