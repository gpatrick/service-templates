"""Example consumer of cls-client."""

from __future__ import annotations

import asyncio

from client_core import APIError, NotFoundError
from cls_client import (
    ACCOUNTS_RETRY,
    PAYMENTS_RETRY,
    AccountsClient,
    PaymentsClient,
    session_token_auth,
)

from settings import settings


def build_clients() -> tuple[AccountsClient, PaymentsClient]:
    """One auth object, shared: Accounts and Payments are one gateway.

    Two retry policies, because reads are safe to repeat and transfers are not.
    """
    auth = session_token_auth(
        settings.auth_url,
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
    )
    accounts = AccountsClient(settings.base_url, auth=auth, retry=ACCOUNTS_RETRY)
    payments = PaymentsClient(settings.base_url, auth=auth, retry=PAYMENTS_RETRY)
    return accounts, payments


async def main() -> None:
    accounts, payments = build_clients()
    try:
        account = await accounts.get_account("12345")
        print(f"account {account.account_id}: {account.name}")

        count = 0
        async for _txn in accounts.iter_transactions("12345"):
            count += 1
        print(f"{count} transactions")

    except NotFoundError:
        print("no such account")
    except APIError as exc:
        print(f"CLS call failed: {exc}")
    finally:
        await accounts.aclose()
        await payments.aclose()


if __name__ == "__main__":
    asyncio.run(main())
