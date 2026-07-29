def total_balance(accounts):
    total = 0
    for account in accounts:
        total += account.balance_cents
    return total


if __name__ == "__main__":
    from data_source import get_accounts, dollars
    print(dollars(total_balance(get_accounts())))