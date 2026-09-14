"""The endpoint surface, exercised through a mock transport.

Same endpoints as the original service client, so anything written against
that works here unchanged apart from the import.
"""

from __future__ import annotations

import httpx
import pytest
from client_core import NotFoundError, ServerError

from cls_client import AccountsClient, PaymentsClient
from cls_client.models.payments import TransferRequest


async def test_get_account_hits_the_right_path(make_client):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"accountId": "12345", "name": "Checking"})

    client = make_client(AccountsClient, handler)
    account = await client.get_account("12345")

    assert seen == ["/accounts/12345"]
    assert account.account_id == "12345"
    await client.aclose()


async def test_a_missing_account_raises_not_found(make_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "nope"})

    client = make_client(AccountsClient, handler)
    with pytest.raises(NotFoundError):
        await client.get_account("nope")
    await client.aclose()


async def test_list_transactions_sends_its_filters(make_client):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"transactions": []})

    client = make_client(AccountsClient, handler)
    await client.list_transactions(
        "12345", start_date="2026-03-01", end_date="2026-03-08", limit=50
    )

    params = seen[0].url.params
    assert params["startDate"] == "2026-03-01"
    assert params["endDate"] == "2026-03-08"
    assert params["limit"] == "50"
    await client.aclose()


async def test_unset_filters_are_not_sent(make_client):
    """None params are dropped rather than serialised as the string "None"."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"transactions": []})

    client = make_client(AccountsClient, handler)
    await client.list_transactions("12345")

    assert "startDate" not in seen[0].url.params
    assert "cursor" not in seen[0].url.params
    await client.aclose()


async def test_iter_transactions_follows_the_cursor(make_client):
    """The paging loop lives in the client so two callers cannot get it
    subtly different."""
    pages = {
        None: {"transactions": [{"transactionId": "t1"}], "nextCursor": "c2"},
        "c2": {"transactions": [{"transactionId": "t2"}], "nextCursor": None},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    client = make_client(AccountsClient, handler)
    ids = [t.transaction_id async for t in client.iter_transactions("12345")]

    assert ids == ["t1", "t2"]
    await client.aclose()


async def test_create_transfer_posts_the_body(make_client):
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(201, json={"transferId": "tr-1", "status": "posted"})

    client = make_client(PaymentsClient, handler)
    result = await client.create_transfer(
        TransferRequest(
            from_account_id="a1", to_account_id="a2", amount="10.00", currency="USD"
        )
    )

    assert result.transfer_id == "tr-1"
    assert b'"10.00"' in bodies[0]  # amount as a string, never a JSON float
    await client.aclose()


async def test_a_transfer_is_never_retried_by_the_transport(make_client):
    """POST is excluded from RetryPolicy unless retry_non_idempotent is set.
    Setting it would let a lost response move money twice."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": "unavailable"})

    client = make_client(PaymentsClient, handler)
    with pytest.raises(ServerError):
        await client.create_transfer(
            TransferRequest(
                from_account_id="a1", to_account_id="a2", amount="1.00", currency="USD"
            )
        )

    assert calls == 1
    await client.aclose()
