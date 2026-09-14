from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest
from conftest import WINDOW
from service_client import AccountsClient, ServerError
from service_client.models.accounts import Transaction

from txn_sync.adapters.destination_http import (
    IDEMPOTENCY_HEADER,
    TRANSACTIONS_PATH,
    HttpTransactionDestination,
    TransactionsClient,
)
from txn_sync.adapters.source_http import (
    HttpTransactionSource,
    _business_date,
    _parse_timestamp,
    _to_source_transaction,
)
from txn_sync.records import DestinationTransaction
from txn_sync.transform import TransformError

# -- source ----------------------------------------------------------------


def upstream(**overrides: object) -> Transaction:
    payload: dict[str, object] = {
        "transactionId": "t1",
        "accountId": "acct-1",
        "amount": "12.34",
        "currency": "USD",
        "description": "coffee",
        "postedAt": "2026-03-03T12:00:00Z",
        "type": "purchase",
    }
    payload.update(overrides)
    return Transaction.model_validate(payload)


def test_an_upstream_transaction_converts_to_the_neutral_record():
    result = _to_source_transaction(upstream(), "acct-1")
    assert result.account_id == "acct-1"
    assert result.transaction_id == "t1"
    assert result.amount == Decimal("12.34")
    assert result.currency == "USD"
    assert result.kind == "purchase"
    assert result.business_date == date(2026, 3, 3)


def test_the_raw_payload_is_carried_for_replay():
    result = _to_source_transaction(upstream(), "acct-1")
    assert result.raw["transactionId"] == "t1"


def test_a_transaction_without_an_id_is_rejected():
    """It could not be given a stable idempotency key, so posting it would
    duplicate on every run."""
    with pytest.raises(TransformError, match="without an id"):
        _to_source_transaction(upstream(transactionId=""), "acct-1")


def test_a_missing_amount_is_rejected():
    with pytest.raises(TransformError, match="amount"):
        _to_source_transaction(upstream(amount=None), "acct-1")


def test_a_missing_currency_is_rejected():
    with pytest.raises(TransformError, match="currency"):
        _to_source_transaction(upstream(currency=None), "acct-1")


def test_a_mismatched_account_is_rejected_rather_than_guessed_at():
    """Everything downstream is scoped by account, so picking one of the two
    silently would corrupt both the key and the checkpoint."""
    with pytest.raises(TransformError, match="claims account"):
        _to_source_transaction(upstream(accountId="acct-9"), "acct-1")


def test_an_absent_upstream_account_falls_back_to_the_one_requested():
    result = _to_source_transaction(upstream(accountId=None), "acct-1")
    assert result.account_id == "acct-1"


def test_a_missing_timestamp_is_rejected():
    """Business date is part of every idempotency key, so it cannot be
    defaulted."""
    with pytest.raises(TransformError, match="business"):
        _to_source_transaction(upstream(postedAt=None), "acct-1")


@pytest.mark.parametrize(
    "raw", ["2026-03-03T12:00:00Z", "2026-03-03T12:00:00+00:00", "2026-03-03T12:00:00"]
)
def test_timestamp_parsing_handles_the_common_iso_spellings(raw: str):
    parsed = _parse_timestamp(raw)
    assert parsed is not None
    assert parsed.date() == date(2026, 3, 3)


def test_an_unparseable_timestamp_becomes_a_transform_error_not_a_crash():
    with pytest.raises(TransformError):
        _to_source_transaction(upstream(postedAt="03/03/2026"), "acct-1")


def test_a_naive_timestamp_is_read_as_utc_rather_than_local_time():
    """A scheduler in one region and a developer in another must derive the
    same business date, because the date is part of the idempotency key."""
    parsed = _parse_timestamp("2026-03-03T23:30:00")
    assert parsed is not None and parsed.tzinfo is None
    assert _business_date(parsed, "acct-1", "t1") == date(2026, 3, 3)


def test_a_timestamp_is_normalized_to_utc_before_the_date_is_taken():
    parsed = _parse_timestamp("2026-03-04T01:30:00+05:00")
    assert parsed is not None
    assert _business_date(parsed, "acct-1", "t1") == date(2026, 3, 3)


async def test_the_source_pushes_the_window_down_to_the_api():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = [upstream().model_dump(by_alias=True, mode="json")]
        return httpx.Response(200, json={"transactions": page})

    client = AccountsClient(
        "https://source.example.com",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://source.example.com",
        ),
    )
    source = HttpTransactionSource(client)

    results = [t async for t in source.fetch("acct-1", WINDOW)]

    assert len(results) == 1
    assert seen[0].url.params["startDate"] == "2026-03-01"
    assert seen[0].url.params["endDate"] == "2026-03-08"
    await source.aclose()


# -- destination ------------------------------------------------------------------


def a_transaction() -> DestinationTransaction:
    return DestinationTransaction(
        account_id="acct-1",
        source_transaction_id="t1",
        business_date=date(2026, 3, 3),
        amount=Decimal("12.34"),
        currency="USD",
        description="coffee",
    )


def destination_with(handler) -> HttpTransactionDestination:
    client = TransactionsClient(
        "https://dest.example.com",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://dest.example.com",
        ),
    )
    return HttpTransactionDestination(client, source_system="src")


async def test_the_idempotency_key_is_sent_as_a_header():
    """If this header name is wrong the destination ignores it, returns 200,
    and every retry creates a duplicate. Nothing fails visibly."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"transactionId": "dest-1"})

    destination = destination_with(handler)
    await destination.post(a_transaction(), idempotency_key="v1-abc")

    assert seen[0].headers[IDEMPOTENCY_HEADER] == "v1-abc"
    assert seen[0].url.path == TRANSACTIONS_PATH
    await destination.aclose()


async def test_the_body_uses_the_destination_field_names():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        bodies.append(_json.loads(request.content))
        return httpx.Response(201, json={"transactionId": "dest-1"})

    destination = destination_with(handler)
    await destination.post(a_transaction(), idempotency_key="v1-abc")

    body = bodies[0]
    assert body["accountId"] == "acct-1"
    assert body["externalReference"] == "t1"
    assert body["businessDate"] == "2026-03-03"
    await destination.aclose()


async def test_amounts_are_serialized_as_strings_not_floats():
    """A JSON float cannot represent most decimal amounts exactly."""
    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content.decode())
        return httpx.Response(201, json={"transactionId": "dest-1"})

    destination = destination_with(handler)
    await destination.post(a_transaction(), idempotency_key="v1-abc")

    assert '"12.34"' in bodies[0]
    await destination.aclose()


async def test_the_destination_id_comes_back_in_the_outcome():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"transactionId": "dest-99"})

    destination = destination_with(handler)
    outcome = await destination.post(a_transaction(), idempotency_key="v1-abc")
    assert outcome.destination_id == "dest-99"
    assert outcome.deduplicated is False
    await destination.aclose()


async def test_a_reported_duplicate_is_surfaced():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"transactionId": "dest-1", "duplicate": True})

    destination = destination_with(handler)
    outcome = await destination.post(a_transaction(), idempotency_key="v1-abc")
    assert outcome.deduplicated is True
    await destination.aclose()


async def test_an_unreported_duplicate_defaults_to_unknown_rather_than_guessed():
    """A 200 versus 201 is a plausible signal but only if the destination's
    documentation actually promises it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"transactionId": "dest-1"})

    destination = destination_with(handler)
    outcome = await destination.post(a_transaction(), idempotency_key="v1-abc")
    assert outcome.deduplicated is False
    await destination.aclose()


async def test_a_write_is_never_retried_by_the_transport():
    """RetryPolicy excludes POST unless retry_non_idempotent is set. Setting
    it on this client would let a lost response become two transactions."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": "unavailable"})

    destination = destination_with(handler)
    with pytest.raises(ServerError):
        await destination.post(a_transaction(), idempotency_key="v1-abc")

    assert calls == 1
    await destination.aclose()
