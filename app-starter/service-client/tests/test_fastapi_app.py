"""Tests for the FastAPI layer in examples/fastapi_app.py.

These cover two things the unit tests cannot: the mapping from upstream errors
onto OUR status codes, and the fact that endpoints return OUR models rather
than the client's. A 401 forwarded to the gateway looks plausible and is
wrong; an upstream field leaking into a response is invisible until a client
regeneration renames it.

Both clients are swapped via dependency_overrides, so lifespan never runs and
no real credentials are needed.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from examples.fastapi_app import app, get_accounts, get_payments
from service_client import (
    AccountsClient,
    PaymentsClient,
    RefreshingBearerAuth,
    TokenFetchError,
)

BASE_URL = "https://api.example.com"


def _client(cls, handler):
    return cls(
        BASE_URL,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BASE_URL
        ),
    )


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    """Lifespan validates these at startup through Settings.

    Set here rather than skipping lifespan, so every test also proves the
    startup path still constructs both clients without exploding. The real
    clients it builds are never used: dependency_overrides takes precedence.

    Settings reads the environment when lifespan runs, not at import, which is
    why monkeypatch works at all. Move it to module level and these fixtures
    would be too late.
    """
    monkeypatch.setenv("SERVICE_BASE_URL", BASE_URL)
    monkeypatch.setenv("SERVICE_TOKEN_URL", f"{BASE_URL}/oauth/token")
    monkeypatch.setenv("SERVICE_CLIENT_ID", "test-id")
    monkeypatch.setenv("SERVICE_CLIENT_SECRET", "test-secret")


@pytest.fixture
def wire():
    """Override both clients, then clean up so overrides do not leak."""

    def default(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accountId": "1"})

    def _wire(*, accounts_handler=None, payments_handler=None) -> TestClient:
        app.dependency_overrides[get_accounts] = lambda: _client(
            AccountsClient, accounts_handler or default
        )
        app.dependency_overrides[get_payments] = lambda: _client(
            PaymentsClient, payments_handler or default
        )
        return TestClient(app)

    yield _wire
    app.dependency_overrides.clear()


def test_read_account_returns_our_model_not_the_upstream_one(wire):
    """Upstream sends camelCase accountId; our contract is snake_case."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/accounts/12345"
        return httpx.Response(
            200,
            json={
                "accountId": "12345",
                "name": "Checking",
                "balance": "1250.75",
                "availableBalance": "1200.00",
            },
        )

    with wire(accounts_handler=handler) as client:
        response = client.get("/accounts/12345")

    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == "12345"
    assert "accountId" not in body
    assert body["available_balance"] == "1200.00"


def test_money_reaches_the_caller_as_a_string(wire):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accountId": "1", "balance": "1250.75"})

    with wire(accounts_handler=handler) as client:
        response = client.get("/accounts/1")

    assert response.json()["balance"] == "1250.75"
    # Not 1250.75 as a JSON number.
    assert '"balance":"1250.75"' in response.text.replace(" ", "")


def test_malformed_upstream_body_becomes_502_not_500(wire):
    """A 200 whose body does not match the client model is the upstream's bug.

    Without ResponseValidationError this escapes as a raw pydantic
    ValidationError and surfaces as a 500, which points the on-call engineer
    at our service rather than at a stale model.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "Checking"})  # no accountId

    with wire(accounts_handler=handler) as client:
        response = client.get("/accounts/1")

    assert response.status_code == 502
    # Upstream detail goes to logs, not to the caller.
    assert "accountId" not in response.text


def test_non_json_upstream_body_becomes_502(wire):
    """An HTML error page from a proxy must not surface as a 500 either."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway error</html>")

    with wire(accounts_handler=handler) as client:
        response = client.get("/accounts/1")

    assert response.status_code == 502


def test_read_transactions_returns_a_page_with_cursor(wire):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "transactionId": "t1",
                        "accountId": "a1",
                        "type": "DEBIT",
                        "postedAt": "2026-01-15",
                    }
                ],
                "nextCursor": "page2",
            },
        )

    with wire(accounts_handler=handler) as client:
        response = client.get("/accounts/1/transactions")

    assert response.status_code == 200
    body = response.json()
    assert body["transactions"][0]["transaction_id"] == "t1"
    assert body["next_cursor"] == "page2"
    # accountId is dropped: the caller supplied it in the path.
    assert "account_id" not in body["transactions"][0]


def test_transaction_cursor_is_passed_upstream(wire):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["cursor"] == "page2"
        return httpx.Response(200, json={"transactions": []})

    with wire(accounts_handler=handler) as client:
        response = client.get("/accounts/1/transactions?cursor=page2")

    assert response.status_code == 200


def test_upstream_404_becomes_404(wire):
    with wire(accounts_handler=lambda r: httpx.Response(404, text="gone")) as client:
        response = client.get("/accounts/missing")

    assert response.status_code == 404


def test_upstream_401_becomes_502_not_401(wire):
    """Our caller must not be told to re-authenticate with the wrong system."""
    with wire(accounts_handler=lambda r: httpx.Response(401, text="bad token")) as c:
        response = c.get("/accounts/1")

    assert response.status_code == 502
    # The upstream body must not be echoed; it may leak internals.
    assert "bad token" not in response.text


def test_upstream_429_becomes_429(wire):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")

    with wire(accounts_handler=handler) as client:
        response = client.get("/accounts/1")

    assert response.status_code == 429


def test_create_transfer_maps_both_directions(wire):
    """Our snake_case request becomes the upstream's camelCase, and back."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/transfers"
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"transferId": "tr-1", "status": "PENDING"})

    body = {
        "from_account_id": "a1",
        "to_account_id": "a2",
        "amount": "25.00",
        "currency": "USD",
    }
    with wire(payments_handler=handler) as client:
        response = client.post("/transfers", json=body)

    # Outbound: translated to the upstream's field names.
    assert sent[0]["fromAccountId"] == "a1"
    assert "from_account_id" not in sent[0]

    # Inbound: translated back to ours.
    assert response.status_code == 201
    assert response.json()["transfer_id"] == "tr-1"


def test_transfer_amount_must_be_positive(wire):
    """Our model validates; a bad amount never reaches the upstream."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(201, json={"transferId": "tr-1"})

    body = {
        "from_account_id": "a1",
        "to_account_id": "a2",
        "amount": "-5.00",
        "currency": "USD",
    }
    with wire(payments_handler=handler) as client:
        response = client.post("/transfers", json=body)

    assert response.status_code == 422
    assert calls == []


def test_invalid_transfer_body_is_rejected_before_the_upstream(wire):
    """FastAPI validates against TransferRequest, so a bad body never leaves."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(201, json={"transferId": "tr-1"})

    with wire(payments_handler=handler) as client:
        response = client.post("/transfers", json={"from_account_id": "a1"})

    assert response.status_code == 422
    assert calls == []


def test_upstream_unreachable_becomes_504(wire):
    """A connection failure is not our bug and not a 502."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with wire(accounts_handler=handler) as client:
        response = client.get("/accounts/1")

    assert response.status_code == 504


def test_token_fetch_failure_becomes_503_not_401(wire):
    """We could not authenticate to the upstream. That is our configuration
    problem, so the caller must not be told their own credentials are bad."""

    async def failing_fetch() -> tuple[str, float | None]:
        raise TokenFetchError("token endpoint unreachable")

    auth = RefreshingBearerAuth(failing_fetch)
    app.dependency_overrides[get_accounts] = lambda: AccountsClient(
        BASE_URL,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"accountId": "1"})
            ),
            base_url=BASE_URL,
            auth=auth,
        ),
    )

    with TestClient(app) as client:
        response = client.get("/accounts/1")

    assert response.status_code == 503


def test_unknown_transaction_type_becomes_502(wire):
    """An unmapped variant is an upstream change, surfaced loudly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"transactions": [{"transactionId": "t1", "type": "INTEREST_ACCRUAL"}]},
        )

    with wire(accounts_handler=handler) as client:
        response = client.get("/accounts/1/transactions")

    assert response.status_code == 502
    assert "INTEREST_ACCRUAL" not in response.text


def test_missing_configuration_fails_at_startup(monkeypatch):
    """Better a clean startup failure than a service that boots green and
    500s on every request.

    Pydantic reports every missing field at once, so one restart tells you
    everything rather than one variable per cycle.
    """
    from pydantic import ValidationError

    for name in (
        "SERVICE_BASE_URL",
        "SERVICE_TOKEN_URL",
        "SERVICE_CLIENT_ID",
        "SERVICE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    from examples.fastapi_app import Settings

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    missing = {error["loc"][0] for error in excinfo.value.errors()}
    assert missing == {"base_url", "token_url", "client_id", "client_secret"}


def test_the_secret_does_not_appear_in_a_repr(monkeypatch):
    """SecretStr is what keeps a credential out of a stray log line."""
    from examples.fastapi_app import Settings

    settings = Settings(_env_file=None)
    assert "test-secret" not in repr(settings)
    assert settings.client_secret.get_secret_value() == "test-secret"
