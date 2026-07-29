def total_balance(accounts):
    total = 0
    for account in accounts:
        total += account.balance_cents
    return total

def spending_by_category(transactions):
    result = {}
    for txn in transactions:
        if txn.amount_cents > 0:
            result[txn.category] = result.get(txn.category, 0) + txn.amount_cents
    return sorted(result.items(), key=lambda x: x[1], reverse=True)

if __name__ == "__main__":
    from data_source import get_accounts, get_transactions, dollars
    print(dollars(total_balance(get_accounts())))
    for category, cents in spending_by_category(get_transactions()):
        print(f"{category:15} {dollars(cents)}")