# Task 4: track, dismiss and restore decisions

Schema **v8**. One new table, `subscription_decisions`, and `subscriptions.py` to drive it.

## Commands

```
python subscriptions.py list
python subscriptions.py track   --account ACCOUNT_ID --merchant MERCHANT
python subscriptions.py dismiss --account ACCOUNT_ID --merchant MERCHANT
python subscriptions.py restore --account ACCOUNT_ID --merchant MERCHANT
```

`list` prints every current candidate and every stored decision with the stored account id to pass back in. The dashboard shows tracked candidates with a `(tracked)` suffix and hides dismissed ones. With no decisions stored, the dashboard render is byte-identical to the baseline.

## Table

`(user_id, account_id, merchant)` is the primary key, the same identity the detector groups on. `decision` is `tracked` or `dismissed`. **Restore deletes the row**, so "undecided" has exactly one representation and a restored candidate is indistinguishable from one never touched. One key never holds two decisions. `dismiss` replaces a `tracked` decision, but `track` **cannot** replace a `dismissed` one: the write is refused inside the database statement and the CLI exits 1 with a message to run `restore` first. The way back is always `restore`, then `track`.

`account_id` references `accounts(id)`. Decisions are not copied per card: tracking Netflix on card A writes one row and leaves card B's key alone.

## Transitions

| Stored decision | `track` | `dismiss` | `restore` |
| --- | --- | --- | --- |
| none (candidate) | tracked | dismissed | fails, nothing stored |
| tracked | no change | dismissed | back to candidate |
| dismissed | **refused, run `restore` first** | no change | back to candidate |

## Merchant names outside the console code page

Output is written with escaping for characters the console cannot encode, so under Windows-1252 `list` shows the merchant 商店 as `\u5546\u5e97`. `--merchant` still requires the exact stored name and rejects the escaped text, so decisions never match a lookalike. To see and copy the real name, run `list` with UTF-8 output. `PYTHONIOENCODING` makes Python emit UTF-8, and `[Console]::OutputEncoding` makes PowerShell decode what it captures as UTF-8. Then copy the **full** account id from the `[id: ...]` part of the line and the exact displayed merchant name into the command:

```powershell
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
python subscriptions.py list
# candidate  商店  $17.99  account card-u  [id: <FULL_ACCOUNT_ID>]
python subscriptions.py dismiss --account "<FULL_ACCOUNT_ID>" --merchant "商店"
```

Replace `<FULL_ACCOUNT_ID>` with the id exactly as `list` printed it, and `商店` with the merchant exactly as displayed. Do not shorten the id.

Terminal-control characters are still escaped in UTF-8 mode. The terminal itself must display UTF-8 for the name to look right; the exact-match rule is unaffected either way.

## Rules

- `track` and `dismiss` require a **current** candidate for that account and merchant, so a typo cannot create a decision for nothing. `restore` only needs a stored decision, so it still works after the detector stops seeing the subscription.
- The account must belong to the user (`repository.owns_account`).
- Decisions never depend on transactions, so reimporting (which upserts by content-based id) and a missing month do not touch them. If a month goes missing the candidate disappears from detection, the decision stays, and `list` shows it as `no longer detected`.

## Migration

`to_v8` does nothing. `init_schema` runs `CREATE TABLE IF NOT EXISTS` before migrations, which creates the new table on any older file, and the registry step exists so the version is stamped and `upgrade` does not refuse the gap. A v6 file walks `7, 8` in one open. See [MIGRATIONS.md](MIGRATIONS.md) section 4 for why DDL must not live inside a step.

## Verification

`tests/test_subscriptions.py` covers each action, per-card independence, persistence across reimport, a missing month, rejected requests, a cross-user account, the v7 upgrade, and the v6 chain with a foreign-key check and a join proving every transaction still resolves to its account. The two-card tests assert that the list is non-empty before comparing statuses.

## Limits

- The merchant is matched exactly as stored (currently `Netflix.Com`). Task 7a changes how it is normalized; after that, decisions made on the old spelling would no longer match and need to be redone.
- There is no API route for decisions, only the CLI.
- The CLI always acts as the local user.
