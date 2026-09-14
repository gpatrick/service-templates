"""Async clients for the Accounts and Payments services.

The generic half of this package moved to `client-core`: BaseClient,
RetryPolicy, the auth helpers and the error taxonomy. Nothing there knows what
an account is, which is the point. Build a NEW service client on that package
directly rather than on this one:

    from client_core import BaseClient, RetryPolicy, client_credentials_auth

What stays here is everything specific to the Accounts and Payments upstream:
the two resource clients, their models, and the two retry constants. Those are
re-exported below so existing imports keep working unchanged.

Both services share a base URL and credentials, so share one auth object
between them. They do NOT share a retry policy: Accounts reads are idempotent
and cheap to retry, while Payments moves money.

    from service_client import (
        AccountsClient, PaymentsClient, RetryPolicy, client_credentials_auth,
    )

    auth = client_credentials_auth(token_url, client_id=..., client_secret=...)

    accounts = AccountsClient(base_url, auth=auth, retry=ACCOUNTS_RETRY)
    payments = PaymentsClient(base_url, auth=auth, retry=PAYMENTS_RETRY)

    account = await accounts.get_account("12345")

ACCOUNTS_RETRY and PAYMENTS_RETRY are sensible defaults exported here. See
examples/fastapi_app.py for wiring into a service and DECISIONS.md for why
the clients are shaped this way.
"""

# Re-exported from client-core so that `from service_client import
# BaseClient` keeps working. New code should import these from
# client_core directly; this package is about Accounts and Payments.
from client_core import (
    APIConnectionError,
    APIError,
    APIStatusError,
    AuthError,
    BaseClient,
    NotFoundError,
    RateLimitError,
    RefreshingBearerAuth,
    ResponseValidationError,
    RetryPolicy,
    ServerError,
    TokenFetchError,
    UpstreamContractError,
    client_credentials_auth,
    maps_upstream,
    require,
)

from .accounts import AccountsClient
from .payments import PaymentsClient

__version__ = "0.1.0"

# Tuned for THESE endpoints, which is why they live here and not in
# client-core. Reads are idempotent and cheap to repeat, so retry a
# little harder. Define your own at your own call site rather than importing
# these; a name that says "accounts" in a service that has none is a leak.
ACCOUNTS_RETRY = RetryPolicy(max_attempts=4, max_elapsed=20.0)

# Money movement. One attempt, no automatic repeat. Raising max_attempts here
# does nothing for POST unless someone also sets retry_non_idempotent=True,
# which would be a serious bug; see DECISIONS.md.
PAYMENTS_RETRY = RetryPolicy(max_attempts=2, max_elapsed=15.0)

__all__ = [
    "ACCOUNTS_RETRY",
    "PAYMENTS_RETRY",
    "AccountsClient",
    "PaymentsClient",
    "BaseClient",
    "RetryPolicy",
    "RefreshingBearerAuth",
    "client_credentials_auth",
    "TokenFetchError",
    "UpstreamContractError",
    "maps_upstream",
    "require",
    "APIError",
    "APIConnectionError",
    "APIStatusError",
    "AuthError",
    "NotFoundError",
    "RateLimitError",
    "ResponseValidationError",
    "ServerError",
]
