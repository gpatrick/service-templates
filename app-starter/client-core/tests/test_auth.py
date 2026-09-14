"""Tests for token refresh.

The fetcher is a plain async callable, so none of these need a network or a
mock patch. Time is controlled via the auth_clock fixture.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from client_core import (
    AuthError,
    RefreshingBearerAuth,
    TokenFetchError,
    client_credentials_auth,
)
from tests.conftest import counting_fetcher


async def test_token_fetched_once_and_reused(auth_clock, make_client):
    fetch, calls = counting_fetcher()
    auth = RefreshingBearerAuth(fetch)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"thingId": "1", "name": "Widget"})

    async with make_client(handler, auth=auth) as api:
        await api.get_thing("1")
        await api.get_thing("2")

    assert len(calls) == 1
    assert seen == ["Bearer token-1"] * 2


async def test_refreshes_inside_leeway_window(auth_clock, make_client, ok_handler):
    fetch, calls = counting_fetcher(lifetime=300.0)
    auth = RefreshingBearerAuth(fetch, leeway=60.0)

    async with make_client(ok_handler, auth=auth) as api:
        await api.get_thing("1")
        auth_clock.advance(250)  # inside the 60s leeway, not yet expired
        await api.get_thing("2")

    assert len(calls) == 2


async def test_does_not_refresh_early(auth_clock, make_client, ok_handler):
    fetch, calls = counting_fetcher(lifetime=300.0)
    auth = RefreshingBearerAuth(fetch, leeway=60.0)

    async with make_client(ok_handler, auth=auth) as api:
        await api.get_thing("1")
        auth_clock.advance(100)  # well outside the leeway window
        await api.get_thing("2")

    assert len(calls) == 1


async def test_missing_expires_in_uses_default(auth_clock, make_client, ok_handler):
    fetch, calls = counting_fetcher(lifetime=None)
    auth = RefreshingBearerAuth(fetch)

    async with make_client(ok_handler, auth=auth) as api:
        await api.get_thing("1")
        auth_clock.advance(3000)  # under the 3600s default
        await api.get_thing("2")

    assert len(calls) == 1


async def test_401_triggers_refresh_and_retries_once(auth_clock, make_client):
    """Covers a token revoked ahead of its stated expiry."""
    fetch, calls = counting_fetcher()
    auth = RefreshingBearerAuth(fetch)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        header = request.headers["Authorization"]
        seen.append(header)
        if header == "Bearer token-1":
            return httpx.Response(401, text="token revoked")
        return httpx.Response(200, json={"thingId": "1", "name": "Widget"})

    async with make_client(handler, auth=auth) as api:
        thing = await api.get_thing("1")

    assert seen == ["Bearer token-1", "Bearer token-2"]
    assert thing.name == "Widget"


async def test_persistent_401_surfaces_as_auth_error(auth_clock, make_client):
    fetch, _ = counting_fetcher()
    auth = RefreshingBearerAuth(fetch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="nope")

    async with make_client(handler, auth=auth) as api:
        with pytest.raises(AuthError):
            await api.get_thing("1")


async def test_concurrent_calls_share_one_refresh(auth_clock, make_client, ok_handler):
    """A burst on a cold cache must fire exactly one token fetch."""
    fetch, calls = counting_fetcher()
    auth = RefreshingBearerAuth(fetch)

    async with make_client(ok_handler, auth=auth) as api:
        await asyncio.gather(*(api.get_thing(str(i)) for i in range(10)))

    assert len(calls) == 1


async def test_invalidate_keeps_newer_token(auth_clock):
    """A late 401 carrying a stale token must not discard its replacement."""
    fetch, calls = counting_fetcher()
    auth = RefreshingBearerAuth(fetch)

    first = await auth.token()
    await auth.invalidate(stale=first)
    second = await auth.token()

    await auth.invalidate(stale=first)  # arrives after the refresh

    assert await auth.token() == second
    assert len(calls) == 2


async def test_fetch_failure_raises_token_fetch_error(
    auth_clock, make_client, ok_handler
):
    async def fetch() -> tuple[str, float | None]:
        raise httpx.ConnectError("token endpoint unreachable")

    async with make_client(ok_handler, auth=RefreshingBearerAuth(fetch)) as api:
        with pytest.raises(TokenFetchError):
            await api.get_thing("1")


async def test_empty_token_raises(auth_clock, make_client, ok_handler):
    async def fetch() -> tuple[str, float | None]:
        return "", 3600.0

    async with make_client(ok_handler, auth=RefreshingBearerAuth(fetch)) as api:
        with pytest.raises(TokenFetchError):
            await api.get_thing("1")


# -- client_credentials_auth ------------------------------------------------
# The real fetcher, as opposed to the fake one every test above injects. This
# is the code most likely to be wrong on first contact with the actual token
# endpoint: form encoding versus JSON, whether expires_in arrives as a string,
# what a 4xx looks like. Better to find out here than during a demo.


def _token_endpoint(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.fixture
def mock_token_endpoint(monkeypatch):
    """Route the bare httpx.AsyncClient inside client_credentials_auth."""

    def _install(handler):
        real_init = httpx.AsyncClient.__init__

        def patched_init(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            real_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    return _install


async def test_client_credentials_sends_form_encoded_grant(mock_token_endpoint):
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        assert request.headers["content-type"].startswith(
            "application/x-www-form-urlencoded"
        )
        return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})

    mock_token_endpoint(handler)
    auth = client_credentials_auth(
        "https://api.example.com/oauth/token",
        client_id="cid",
        client_secret="secret",
    )

    assert await auth.token() == "abc"
    body = seen[0].decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=cid" in body


async def test_client_credentials_includes_scope_when_given(mock_token_endpoint):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        return httpx.Response(200, json={"access_token": "abc"})

    mock_token_endpoint(handler)
    auth = client_credentials_auth(
        "https://api.example.com/oauth/token",
        client_id="cid",
        client_secret="secret",
        scope="accounts.read",
    )
    await auth.token()

    assert "scope=accounts.read" in seen[0]


async def test_client_credentials_handles_string_expires_in(mock_token_endpoint):
    """Some servers send expires_in as a JSON string rather than a number."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "abc", "expires_in": "3600"})

    mock_token_endpoint(handler)
    auth = client_credentials_auth(
        "https://api.example.com/oauth/token", client_id="c", client_secret="s"
    )

    assert await auth.token() == "abc"


async def test_client_credentials_4xx_raises_token_fetch_error(mock_token_endpoint):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid_client")

    mock_token_endpoint(handler)
    auth = client_credentials_auth(
        "https://api.example.com/oauth/token", client_id="c", client_secret="bad"
    )

    with pytest.raises(TokenFetchError) as excinfo:
        await auth.token()

    assert "401" in str(excinfo.value)


async def test_client_credentials_missing_access_token_raises(mock_token_endpoint):
    """A 200 with the wrong body shape must not yield a None token."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": "abc"})  # wrong key

    mock_token_endpoint(handler)
    auth = client_credentials_auth(
        "https://api.example.com/oauth/token", client_id="c", client_secret="s"
    )

    with pytest.raises(TokenFetchError):
        await auth.token()
