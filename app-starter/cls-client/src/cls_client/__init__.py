"""Typed async clients for the CLS backend.

One vendor package on top of client-core. client_core owns transport, retries,
error mapping and response validation; everything here is CLS-specific: its
endpoints, its models, its auth handshake, and retry budgets tuned to it.

    from client_core import RetryPolicy
    from cls_client import ACCOUNTS_RETRY, AccountsClient, session_token_auth

    auth = session_token_auth(
        auth_base_url,
        client_id=settings.cls_client_id,
        client_secret=settings.cls_client_secret.get_secret_value(),
    )

    async with AccountsClient(base_url, auth=auth, retry=ACCOUNTS_RETRY) as accounts:
        account = await accounts.get_account("12345")
        async for txn in accounts.iter_transactions("12345"):
            ...

ONE AUTH OBJECT PER BACKEND. Accounts and Payments here share a gateway, so
they share one. Never share across vendors: that sends CLS's token to someone
else, which at best 401s and at worst succeeds somewhere it should not have.

Error classes and RetryPolicy come from client_core; import them from there
rather than expecting this package to re-export them.
"""

from client_core import RetryPolicy

from .accounts import AccountsClient
from .auth import SESSION_PATH, TOKEN_PATH, session_token_auth
from .payments import PaymentsClient

__version__ = "0.1.0"

# Tuned for THESE endpoints, which is why they live here and not in
# client-core. Reads are idempotent and cheap to repeat, so they retry harder.
ACCOUNTS_RETRY = RetryPolicy(max_attempts=4, max_elapsed=20.0)

# Anything under Payments moves money. One attempt, and POST is excluded from
# retries anyway unless retry_non_idempotent is set. Do not set it.
PAYMENTS_RETRY = RetryPolicy(max_attempts=2, max_elapsed=15.0)

__all__ = [
    "ACCOUNTS_RETRY",
    "PAYMENTS_RETRY",
    "SESSION_PATH",
    "TOKEN_PATH",
    "AccountsClient",
    "PaymentsClient",
    "session_token_auth",
]
