"""Tests for the generic transport layer.

These should survive any change to endpoints.py or models.py. If a change to
the upstream API breaks a test in this file, something API-specific has
leaked into transport.py.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

from client_core import (
    APIError,
    APIStatusError,
    AuthError,
    NotFoundError,
    RateLimitError,
    ResponseValidationError,
    RetryPolicy,
    ServerError,
)
from client_core.transport import BaseClient


async def test_success_returns_validated_model(make_client):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/things/12345"
        return httpx.Response(200, json={"thingId": "12345", "name": "Widget"})

    async with make_client(handler) as api:
        thing = await api.get_thing("12345")

    assert thing.thing_id == "12345"


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, AuthError),
        (403, AuthError),
        (404, NotFoundError),
        (429, RateLimitError),
        (500, ServerError),
        (418, APIStatusError),
    ],
)
async def test_status_codes_map_to_exceptions(make_client, status, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="upstream said no")

    # One attempt so retryable statuses surface immediately.
    async with make_client(handler, retry=RetryPolicy(max_attempts=1)) as api:
        with pytest.raises(expected) as excinfo:
            await api.get_thing("1")

    assert excinfo.value.status_code == status
    assert "upstream said no" in str(excinfo.value)


async def test_error_carries_request_id(make_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, headers={"x-request-id": "abc-123"}, text="boom")

    async with make_client(handler, retry=RetryPolicy(max_attempts=1)) as api:
        with pytest.raises(ServerError) as excinfo:
            await api.get_thing("1")

    assert excinfo.value.request_id == "abc-123"
    assert "abc-123" in str(excinfo.value)


async def test_retries_then_succeeds(make_client):
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"thingId": "1", "name": "Widget"})

    async with make_client(handler, retry=RetryPolicy(max_attempts=3)) as api:
        thing = await api.get_thing("1")

    assert len(calls) == 3
    assert thing.name == "Widget"


async def test_retry_budget_stops_before_gateway_timeout(make_client):
    """With the elapsed budget spent, raise the last response rather than
    sleeping into a gateway timeout nobody is waiting on."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="unavailable")

    policy = RetryPolicy(max_attempts=5, max_elapsed=0.0)
    async with make_client(handler, retry=policy) as api:
        with pytest.raises(ServerError):
            await api.get_thing("1")

    assert calls == [1]


async def test_post_is_not_retried_by_default(make_client):
    """A retried POST after a timeout can duplicate a create.

    Tested at the transport layer here; tests/test_payments.py pins the same
    behavior for the transfer endpoint specifically.
    """
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="unavailable")

    async with make_client(handler) as api:
        with pytest.raises(ServerError):
            await api.raw("POST", "/anything", json={"k": "v"})

    assert calls == [1]


async def test_connection_errors_are_retried(make_client):
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 2:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"thingId": "1", "name": "Widget"})

    async with make_client(handler, retry=RetryPolicy(max_attempts=3)) as api:
        thing = await api.get_thing("1")

    assert len(calls) == 2
    assert thing.name == "Widget"


async def test_retry_after_header_is_honored(make_client):
    policy = RetryPolicy(max_attempts=2, max_delay=5.0)
    assert policy.delay(1, httpx.Response(429, headers={"Retry-After": "3"})) == 3.0
    # Capped at max_delay.
    assert policy.delay(1, httpx.Response(429, headers={"Retry-After": "99"})) == 5.0


async def test_none_params_are_dropped(make_client):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "limit" not in request.url.params
        assert request.url.params["cursor"] == "abc"
        return httpx.Response(200, json={"things": []})

    async with make_client(handler) as api:
        await api.list_things(limit=None, cursor="abc")


async def test_injected_client_is_not_closed(make_client, ok_handler):
    """The client only closes transports it created."""
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(ok_handler), base_url="https://api.example.com"
    )
    from conftest import DemoClient

    api = DemoClient("https://api.example.com", http_client=http_client)
    await api.aclose()

    assert not http_client.is_closed
    await http_client.aclose()


async def test_requires_exactly_one_credential():
    from conftest import DemoClient

    with pytest.raises(ValueError):
        DemoClient("https://api.example.com")
    with pytest.raises(ValueError):
        DemoClient("https://api.example.com", token="x", auth=httpx.Auth())


async def test_malformed_2xx_body_raises_response_validation_error(make_client):
    """A 200 that does not match the model must not leak pydantic's error.

    Callers catch APIError and its subclasses; a bare ValidationError slips
    through that net and lands as an unhandled 500.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "Widget"})  # no thingId

    async with make_client(handler) as api:
        with pytest.raises(ResponseValidationError) as excinfo:
            await api.get_thing("1")

    error = excinfo.value
    assert isinstance(error, APIError)
    assert error.model == "Thing"
    # The raw body is carried, since the validation message alone rarely says
    # enough to diagnose a real response.
    assert b"Widget" in error.body


async def test_non_json_2xx_body_raises_response_validation_error(make_client):
    """An HTML error page served with a 200 by a proxy hits the same path."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway error</html>")

    async with make_client(handler) as api:
        with pytest.raises(ResponseValidationError):
            await api.get_thing("1")


# --------------------------------------------------------------------------
# Per-request headers
#
# Added for the txn-sync ETL job, which has to send a per-request idempotency
# key. The tests are here rather than in that project because the behavior is
# this module's.
# --------------------------------------------------------------------------


async def test_post_sends_per_request_headers():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    class Thing(BaseModel):
        ok: bool

    client = BaseClient(
        "https://api.example.com",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.example.com",
        ),
    )
    await client._post("/things", Thing, body=None, headers={"Idempotency-Key": "k1"})

    assert seen[0].headers["Idempotency-Key"] == "k1"


async def test_per_request_headers_are_not_sent_as_query_parameters():
    """The failure this named parameter prevents.

    Without it, headers={...} would fall into **params, become part of the
    query string, and the header would never be sent. Nothing would error: an
    API ignores an unrecognized query parameter and returns 200, so an
    idempotency key would appear to be in use while every retry created a
    duplicate.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    class Thing(BaseModel):
        ok: bool

    client = BaseClient(
        "https://api.example.com",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.example.com",
        ),
    )
    await client._post("/things", Thing, body=None, headers={"Idempotency-Key": "k1"})

    assert "headers" not in seen[0].url.params
    assert "Idempotency-Key" not in seen[0].url.params


async def test_omitting_headers_still_works():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    class Thing(BaseModel):
        ok: bool

    client = BaseClient(
        "https://api.example.com",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.example.com",
        ),
    )
    assert (await client._post("/things", Thing, body=None)).ok is True
