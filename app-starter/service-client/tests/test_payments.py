"""Tests for the Payments service. API-SPECIFIC, expect to rewrite these.

The retry test in here is the most important one in the repository. Read its
docstring before changing anything about how POST is handled.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from service_client import PAYMENTS_RETRY, RetryPolicy, ServerError
from service_client.models.payments import TransferRequest


def a_transfer(**overrides) -> TransferRequest:
    fields = {
        "from_account_id": "a1",
        "to_account_id": "a2",
        "amount": Decimal("25.00"),
        "currency": "USD",
    }
    fields.update(overrides)
    return TransferRequest(**fields)


async def test_create_transfer_sends_wire_names(make_payments):
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/transfers"
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"transferId": "tr-1", "status": "PENDING"})

    async with make_payments(handler) as payments:
        transfer = await payments.create_transfer(a_transfer())

    assert sent[0]["fromAccountId"] == "a1"
    assert sent[0]["toAccountId"] == "a2"
    assert "from_account_id" not in sent[0]
    assert transfer.transfer_id == "tr-1"
    assert transfer.status == "PENDING"


async def test_amount_serializes_without_float_error(make_payments):
    """Decimal must not become 0.30000000000000004 on the way out."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content.decode())
        return httpx.Response(201, json={"transferId": "tr-1"})

    async with make_payments(handler) as payments:
        await payments.create_transfer(a_transfer(amount=Decimal("0.30")))

    assert "0.30" in sent[0]
    assert "0.30000" not in sent[0]


async def test_unset_optional_body_fields_are_omitted(make_payments):
    """exclude_unset: do not send keys the caller never set."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"transferId": "tr-1"})

    async with make_payments(handler) as payments:
        await payments.create_transfer(a_transfer())

    assert "description" not in sent[0]


async def test_transfer_is_never_retried(make_payments):
    """THE IMPORTANT ONE. A retried POST after a timeout can move money twice.

    The request may have succeeded with only the response lost, so a repeat
    creates a second transfer. RetryPolicy.retry_non_idempotent must stay
    False on this client. If someone sets it True, this test fails.

    Do not "fix" this test by loosening the assertion. If the API gains an
    idempotency key, the correct change is to send the key and reuse it
    across retries, not to enable blind retrying.
    """
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="unavailable")

    async with make_payments(handler, retry=PAYMENTS_RETRY) as payments:
        with pytest.raises(ServerError):
            await payments.create_transfer(a_transfer())

    assert calls == [1]


async def test_transfer_not_retried_even_with_a_permissive_policy(make_payments):
    """Attempt count alone must not enable POST retries."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="unavailable")

    permissive = RetryPolicy(max_attempts=5, max_elapsed=60.0)
    async with make_payments(handler, retry=permissive) as payments:
        with pytest.raises(ServerError):
            await payments.create_transfer(a_transfer())

    assert calls == [1]


async def test_missing_required_field_fails_before_the_request(make_payments):
    """Validation happens at construction, not at the API."""
    with pytest.raises(ValidationError):
        TransferRequest(from_account_id="a1", amount=Decimal("1"), currency="USD")


async def test_transfer_response_parses_with_minimal_body(make_payments):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"transferId": "tr-1"})

    async with make_payments(handler) as payments:
        transfer = await payments.create_transfer(a_transfer())

    assert transfer.transfer_id == "tr-1"
    assert transfer.status is None
