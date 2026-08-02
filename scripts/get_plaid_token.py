"""
scripts/get_plaid_token.py
--------------------------
RUN ONCE. Obtain a sandbox access_token and paste it into .env.

    ./venv/Scripts/python.exe scripts/get_plaid_token.py
    ./venv/Scripts/python.exe scripts/get_plaid_token.py --record

This is deliberately NOT part of the adapter, and the reason is worth stating:
an access token does not expire. It is obtained once, by a human, and stored --
so making the adapter capable of minting one would give it a code path that
runs approximately never, holds the client_id/secret at runtime, and exists
only to paper over an empty .env. The adapter should read PLAID_ACCESS_TOKEN
and fail loudly when it is missing.

WHY THERE IS NO BROWSER HERE
In production a user links their bank through Plaid Link, a JS widget that
hands back a short-lived public_token. Sandbox lets you skip all of it:
/sandbox/public_token/create mints a valid public_token for any institution id
and product list, and from there the exchange is identical to production. So
the handshake below is the real one minus the browser:

    /sandbox/public_token/create  ->  public_token   (sandbox-only shortcut)
    /item/public_token/exchange   ->  access_token   (identical in production)

ins_109508 is a plain non-OAuth US test bank. OAuth institutions in sandbox
require a redirect flow that this shortcut cannot fake, which is the usual
reason a copied snippet fails here.

SECURITY
The access_token grants read access to the linked Item until you remove it.
This script PRINTS it, because that is the only way to get it into your hands
-- so do not run it on a shared screen, and remember it lands in your shell
scrollback. It never prints PLAID_SECRET. Nothing here writes .env for you:
a script that edits a gitignored credentials file is a script that can
silently clobber one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import plaid
from dotenv import load_dotenv
from plaid.api import plaid_api
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)
from plaid.model.products import Products
from plaid.model.sandbox_public_token_create_request import (
    SandboxPublicTokenCreateRequest,
)
from plaid.model.transactions_sync_request import TransactionsSyncRequest

#: A non-OAuth US test bank. See the module docstring for why OAuth ones fail.
INSTITUTION_ID = "ins_109508"

#: Products the Item is created with. Asking for a product you did not enable
#: on the Item is an error later, not a silent empty list.
INITIAL_PRODUCTS = ["transactions"]

#: Where --record writes the raw payloads. Committed on purpose: it is what
#: lets tests/test_conformance.py exercise the Plaid adapter's TRANSLATION
#: without credentials, on every run, deterministically.
FIXTURE_PATH = Path("fixtures/plaid_sandbox.json")

#: Sandbox generates transactions asynchronously; the first sync after Item
#: creation routinely returns PRODUCT_NOT_READY for a few seconds.
READY_ATTEMPTS = 10
READY_SLEEP_SECONDS = 3


def _client() -> plaid_api.PlaidApi:
    """Build an API client from .env, failing loudly on anything missing.

    `load_dotenv()` does NOT override variables already set in the real
    environment -- so an exported PLAID_ENV wins over the file, which is the
    behaviour you want when overriding for one command.
    """
    load_dotenv()

    client_id = os.environ.get("PLAID_CLIENT_ID", "").strip()
    secret = os.environ.get("PLAID_SECRET", "").strip()
    env = os.environ.get("PLAID_ENV", "sandbox").strip().lower()

    # Checked for EMPTINESS, not presence. .env.example ships these keys with
    # empty values, so `"PLAID_CLIENT_ID" in os.environ` is true for a config
    # that was copied and never filled in.
    missing = [
        name for name, value in
        (("PLAID_CLIENT_ID", client_id), ("PLAID_SECRET", secret))
        if not value
    ]
    if missing:
        sys.exit(
            f"error: {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} empty or unset.\n"
            f"  Copy .env.example to .env and fill in the values from\n"
            f"  https://dashboard.plaid.com -> Developers > Keys.\n"
            f"  Use the SANDBOX secret, not development or production."
        )

    if env != "sandbox":
        # plaid-python 42 removed the Development host entirely; only Sandbox
        # and Production exist. Refusing here is not paranoia -- this script
        # mints Items, and /sandbox/public_token/create against production
        # credentials is a request that should never be attempted.
        sys.exit(
            f"error: PLAID_ENV={env!r}. This script only runs against sandbox.\n"
            f"  (plaid-python 42 offers Sandbox and Production only -- the\n"
            f"  'development' environment no longer exists.)"
        )

    configuration = plaid.Configuration(
        host=plaid.Environment.Sandbox,
        api_key={"clientId": client_id, "secret": secret},
    )
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))


def _mint_access_token(client: plaid_api.PlaidApi) -> tuple[str, str]:
    """The two-step handshake. Returns (access_token, item_id)."""
    created = client.sandbox_public_token_create(
        SandboxPublicTokenCreateRequest(
            institution_id=INSTITUTION_ID,
            initial_products=[Products(p) for p in INITIAL_PRODUCTS],
        )
    )
    exchanged = client.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=created["public_token"])
    )
    return exchanged["access_token"], exchanged["item_id"]


def _sync_all(client: plaid_api.PlaidApi, access_token: str) -> list:
    """Every added transaction, following the cursor to the end.

    /transactions/sync is paginated and `has_more` is the ONLY correct stop
    condition -- an empty page is not the end, and neither is a short one.
    """
    added, cursor = [], None
    while True:
        request = TransactionsSyncRequest(access_token=access_token, count=500)
        if cursor is not None:
            request.cursor = cursor
        response = client.transactions_sync(request)
        added.extend(response["added"])
        cursor = response["next_cursor"]
        if not response["has_more"]:
            return added


def _sync_when_ready(client: plaid_api.PlaidApi, access_token: str) -> list:
    """Retry past PRODUCT_NOT_READY, which is expected, not exceptional.

    Sandbox builds an Item's transaction history in the background. Treating
    the first failure as fatal is the single most common reason this script
    "does not work" on a freshly created Item.
    """
    for attempt in range(1, READY_ATTEMPTS + 1):
        try:
            return _sync_all(client, access_token)
        except plaid.ApiException as exc:
            if "PRODUCT_NOT_READY" not in str(exc.body):
                raise
            print(f"  transactions not ready yet "
                  f"(attempt {attempt}/{READY_ATTEMPTS}), waiting...")
            time.sleep(READY_SLEEP_SECONDS)

    sys.exit(
        "error: transactions were still not ready after "
        f"{READY_ATTEMPTS * READY_SLEEP_SECONDS}s. The access_token above is "
        "still valid -- re-run with --record to retry just the recording."
    )


def _record(client: plaid_api.PlaidApi, access_token: str) -> None:
    """Save raw payloads as the conformance fixture.

    Recorded HERE rather than in a later step on purpose: this is the one
    moment a valid token is guaranteed to be in hand, and a fixture captured
    now means the Plaid adapter's translation can be tested by anyone, forever,
    with no credentials and no network.

    THE TOKEN IS STRIPPED. It is a credential and this file is committed.
    Sandbox account ids and balances are fabricated, so those are fine.
    """
    print("\nrecording sandbox payloads...")
    accounts = client.accounts_get(AccountsGetRequest(access_token=access_token))
    transactions = _sync_when_ready(client, access_token)

    payload = {
        "_comment": (
            "Recorded from Plaid sandbox by scripts/get_plaid_token.py. "
            "Fabricated data, no credentials. Sandbox generates dates relative "
            "to the recording date, so do not assert on absolute dates."
        ),
        "_institution_id": INSTITUTION_ID,
        "accounts": [a.to_dict() for a in accounts["accounts"]],
        "transactions": [t.to_dict() for t in transactions],
    }
    payload.pop("access_token", None)

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # default=str because the SDK deserializes dates into `date` objects, which
    # json cannot encode. ISO strings are what the raw API sent anyway.
    FIXTURE_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    print(f"  wrote {FIXTURE_PATH} "
          f"({len(payload['accounts'])} accounts, "
          f"{len(payload['transactions'])} transactions)")


def main() -> int:
    # Not `description=__doc__`: argparse reflows whitespace, which turns the
    # module docstring into an unreadable wall. The docstring is for readers of
    # the file; --help gets one line.
    parser = argparse.ArgumentParser(
        description="Mint a Plaid SANDBOX access_token to paste into .env. Run once."
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="also save the raw sandbox payloads to fixtures/plaid_sandbox.json, "
             "for credential-free conformance tests",
    )
    args = parser.parse_args()

    client = _client()
    print(f"creating a sandbox Item at {INSTITUTION_ID}...")
    access_token, item_id = _mint_access_token(client)

    if args.record:
        _record(client, access_token)

    print("\n" + "=" * 68)
    print("Paste this into .env (it does not expire, so only once):\n")
    print(f"PLAID_ACCESS_TOKEN={access_token}")
    print(f"\n  item_id: {item_id}   (not a secret; useful in support tickets)")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
