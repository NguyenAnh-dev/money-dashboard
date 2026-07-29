from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from data_source import get_accounts, get_transactions, dollars

console = Console()


def show_accounts(accounts):
    table = Table(title="Accounts", title_style="bold", header_style="bold cyan")
    table.add_column("Institution")
    table.add_column("Account")
    table.add_column("Balance", justify="right")

    for a in accounts:
        style = "red" if a.balance_cents < 0 else "green"
        table.add_row(a.institution, a.name,
                      f"[{style}]{dollars(a.balance_cents)}[/{style}]")

    console.print(table)

    net = sum(a.balance_cents for a in accounts)
    style = "red" if net < 0 else "green"
    console.print(Panel(f"[{style} bold]{dollars(net)}[/{style} bold]",
                        title="Net Worth (across all banks)", expand=False))


def show_recent_transactions(transactions, limit=10):
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


def main():
    accounts = get_accounts()
    transactions = get_transactions()

    console.print()
    console.print("[bold]  MONEY DASHBOARD[/bold]  [dim]v0.1 - fake data[/dim]")
    console.print()

    show_accounts(accounts)
    console.print()
    show_recent_transactions(transactions)
    console.print()
    console.print(f"[dim]{len(transactions)} transactions across "
                  f"{len(accounts)} accounts at "
                  f"{len({a.institution for a in accounts})} institutions[/dim]")
    console.print()


if __name__ == "__main__":
    main()
