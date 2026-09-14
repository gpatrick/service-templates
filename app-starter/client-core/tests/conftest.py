"""Shared fixtures for the transport layer.

Everything goes through httpx.MockTransport rather than patching internals, so
refactoring BaseClient does not break the tests. If a test needs mock.patch on
an underscored attribute, that is a signal the seam is in the wrong place, not
a reason to patch.

DemoClient below is a deliberately boring subclass. This package has no domain,
so the tests need some concrete client to exercise the base class through, and
inventing a two-endpoint one here is better than depending on a real API's
client and dragging its vocabulary back in.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field

from client_core import BaseClient

BASE_URL = "https://api.example.com"


class Thing(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    thing_id: str = Field(alias="thingId")
    name: str | None = None


class ThingList(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    things: list[Thing] = Field(default_factory=list)
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class DemoClient(BaseClient):
    """One GET, one list, one POST. Enough to exercise everything in BaseClient."""

    async def get_thing(self, thing_id: str) -> Thing:
        return await self._get(f"/things/{thing_id}", Thing)

    async def list_things(
        self, *, limit: int | None = None, cursor: str | None = None
    ) -> ThingList:
        return await self._get("/things", ThingList, limit=limit, cursor=cursor)

    async def create_thing(self, body: Thing) -> Thing:
        return await self._post("/things", Thing, body=body)


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
def make_client():
    """Build a DemoClient wired to a MockTransport request handler."""

    def _make(handler, **kwargs) -> DemoClient:
        return _wire(DemoClient, handler, **kwargs)

    return _make


@pytest.fixture
def ok_handler():
    """A minimal valid response, for tests that do not care about the body."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"thingId": "1", "name": "Widget"})

    return handler


def counting_fetcher(lifetime: float | None = 3600.0):
    """An async token fetcher that issues token-1, token-2, ... on demand."""
    calls: list[str] = []

    async def fetch() -> tuple[str, float | None]:
        token = f"token-{len(calls) + 1}"
        calls.append(token)
        return token, lifetime

    return fetch, calls
