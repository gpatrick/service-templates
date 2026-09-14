"""Tests for how the two clients are wired together.

These cover the composition decisions rather than either service's API:
shared auth, separate pools, separate retry policies.
"""

from __future__ import annotations

import httpx

from service_client import (
    ACCOUNTS_RETRY,
    PAYMENTS_RETRY,
    AccountsClient,
    PaymentsClient,
    RefreshingBearerAuth,
)
from tests.conftest import BASE_URL, counting_fetcher


async def test_shared_auth_fetches_one_token_for_both_clients(auth_clock):
    """Both services accept the same credential, so one token serves both."""
    fetch, calls = counting_fetcher()
    auth = RefreshingBearerAuth(fetch)
    seen: list[str] = []

    def accounts_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"accountId": "1"})

    def payments_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(201, json={"transferId": "tr-1"})

    accounts = AccountsClient(
        BASE_URL,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(accounts_handler),
            base_url=BASE_URL,
            auth=auth,
        ),
    )
    payments = PaymentsClient(
        BASE_URL,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(payments_handler),
            base_url=BASE_URL,
            auth=auth,
        ),
    )

    await accounts.get_account("1")
    await payments.raw("GET", "/transfers/tr-1")

    assert len(calls) == 1
    assert seen == ["Bearer token-1", "Bearer token-1"]


async def test_clients_have_independent_retry_policies(auth_clock):
    """Accounts retries reads; Payments does not retry writes."""
    assert ACCOUNTS_RETRY.max_attempts > PAYMENTS_RETRY.max_attempts
    assert ACCOUNTS_RETRY.retry_non_idempotent is False
    assert PAYMENTS_RETRY.retry_non_idempotent is False


async def test_separate_pools_are_not_shared(ok_handler):
    """Each client owns its own transport unless one is injected.

    Pool isolation is the point: a burst of Accounts traffic must not consume
    every connection and stall money movement behind it.
    """
    accounts = AccountsClient(BASE_URL, token="x")
    payments = PaymentsClient(BASE_URL, token="x")

    assert accounts._client is not payments._client

    await accounts.aclose()
    assert not payments._client.is_closed

    await payments.aclose()


async def test_injected_transport_is_not_closed_by_the_client(ok_handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(ok_handler), base_url=BASE_URL)
    accounts = AccountsClient(BASE_URL, http_client=http)

    await accounts.aclose()

    assert not http.is_closed
    await http.aclose()


# --------------------------------------------------------------------------
# Backward compatibility after the client-core split
#
# The generic half moved out. These re-exports are what keep existing imports
# working, so they are asserted rather than assumed. If one of these fails,
# something downstream broke at import time, which is the worst place to find
# out.
# --------------------------------------------------------------------------


def test_generic_names_are_still_importable_from_service_client():
    import client_core

    import service_client

    for name in (
        "BaseClient",
        "RetryPolicy",
        "client_credentials_auth",
        "RefreshingBearerAuth",
        "TokenFetchError",
        "UpstreamContractError",
        "require",
        "APIError",
        "APIConnectionError",
        "APIStatusError",
        "AuthError",
        "NotFoundError",
        "RateLimitError",
        "ResponseValidationError",
        "ServerError",
    ):
        assert hasattr(service_client, name), f"{name} no longer re-exported"
        assert getattr(service_client, name) is getattr(client_core, name), (
            f"{name} is a different object in each package"
        )


def test_client_core_knows_nothing_about_this_api():
    """The whole point of the split. If this fails, domain vocabulary has
    leaked back into the generic package."""
    import client_core

    for name in ("AccountsClient", "PaymentsClient", "ACCOUNTS_RETRY", "PAYMENTS_RETRY"):
        assert not hasattr(client_core, name), f"{name} leaked into transport"
