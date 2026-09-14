"""Shared fixtures.

Everything here goes through httpx.MockTransport rather than patching
internals, so refactoring the clients does not break the tests. If a test
needs mock.patch on an underscored attribute, that is a signal the seam is in
the wrong place, not a reason to patch.
"""

from __future__ import annotations

import httpx
import pytest

from service_client import AccountsClient, PaymentsClient

BASE_URL = "https://api.example.com"


class FakeClock:
    """Controls time.monotonic inside a module under test."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def auth_clock(monkeypatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr("client_core.auth.time.monotonic", clock)
    return clock


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Make backoff instant so retry tests do not actually wait."""

    async def instant(_seconds):
        return None

    monkeypatch.setattr("client_core.transport.asyncio.sleep", instant)


def _wire(cls, handler, *, auth=None, **kwargs):
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=BASE_URL,
        auth=auth,
        headers={} if auth else {"Authorization": "Bearer test-token"},
    )
    return cls(BASE_URL, http_client=http_client, **kwargs)


@pytest.fixture
def make_accounts():
    """Build an AccountsClient wired to a MockTransport request handler."""

    def _make(handler, **kwargs) -> AccountsClient:
        return _wire(AccountsClient, handler, **kwargs)

    return _make


@pytest.fixture
def make_payments():
    """Build a PaymentsClient wired to a MockTransport request handler."""

    def _make(handler, **kwargs) -> PaymentsClient:
        return _wire(PaymentsClient, handler, **kwargs)

    return _make


@pytest.fixture
def ok_handler():
    """A minimal valid Accounts response, for tests that do not care."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accountId": "1", "name": "Checking"})

    return handler


def counting_fetcher(lifetime: float | None = 3600.0):
    """An async token fetcher that issues token-1, token-2, ... on demand."""
    calls: list[str] = []

    async def fetch() -> tuple[str, float | None]:
        token = f"token-{len(calls) + 1}"
        calls.append(token)
        return token, lifetime

    return fetch, calls
