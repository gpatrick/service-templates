"""Tests for the CLS session-then-token handshake.

All through httpx.MockTransport, so the whole flow runs with no network. The
cookie behaviour is the part worth testing hardest: it is invisible in the
code, it is the thing that breaks when someone "tidies up" the fetcher into
two separate clients, and the symptom is a 401 that looks like bad
credentials.
"""

from __future__ import annotations

import httpx
import pytest
from client_core import TokenFetchError

from cls_client.auth import session_token_auth

BASE = "https://cls.example.com"


def handshake_handler(
    *,
    set_cookie: bool = True,
    session_status: int = 200,
    token_status: int = 200,
    token_body: dict | None = None,
    log: list[str] | None = None,
):
    """A MockTransport handler that plays the two-step server."""

    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(request.url.path)

        if request.url.path == "/auth/session":
            headers = {"set-cookie": "CLSSESSION=abc123; Path=/"} if set_cookie else {}
            return httpx.Response(session_status, json={"ok": True}, headers=headers)

        if request.url.path == "/auth/token":
            # The server only issues a token to a request carrying the cookie.
            if "CLSSESSION" not in request.headers.get("cookie", ""):
                return httpx.Response(401, json={"error": "no session"})
            body = token_body if token_body is not None else {
                "access_token": "tok-1",
                "expires_in": 900,
            }
            return httpx.Response(token_status, json=body)

        return httpx.Response(404, json={"error": request.url.path})

    return handler


@pytest.fixture
def patched_transport(monkeypatch):
    """Route every AsyncClient the fetcher builds through a MockTransport.

    Patching the transport rather than the client keeps the real
    AsyncClient, and therefore the real cookie jar, which is the behaviour
    under test. A fake client would make these tests pass for the wrong
    reason.
    """

    def install(handler):
        real_init = httpx.AsyncClient.__init__

        def patched(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            real_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)

    return install


async def test_the_handshake_returns_a_token(patched_transport):
    patched_transport(handshake_handler())
    auth = session_token_auth(BASE, client_id="id", client_secret="secret")

    token, lifetime = await auth._fetch()

    assert token == "tok-1"
    assert lifetime == 900


async def test_the_session_cookie_is_carried_to_the_token_call(patched_transport):
    """The reason both calls share one AsyncClient.

    The mock server refuses to issue a token without the cookie, so a passing
    token here proves httpx carried it. Split the fetcher into two clients and
    this fails.
    """
    calls: list[str] = []
    patched_transport(handshake_handler(log=calls))
    auth = session_token_auth(BASE, client_id="id", client_secret="secret")

    token, _ = await auth._fetch()

    assert token == "tok-1"
    assert calls == ["/auth/session", "/auth/token"]


async def test_a_session_that_sets_no_cookie_fails_with_a_useful_message(
    patched_transport,
):
    """Without this check the failure surfaces as a bare 401 on the token call,
    which reads as bad credentials and sends you looking in the wrong place."""
    patched_transport(handshake_handler(set_cookie=False))
    auth = session_token_auth(BASE, client_id="id", client_secret="secret")

    with pytest.raises(TokenFetchError, match="set no cookie"):
        await auth._fetch()


async def test_a_failed_session_step_names_the_step(patched_transport):
    patched_transport(handshake_handler(session_status=403))
    auth = session_token_auth(BASE, client_id="id", client_secret="secret")

    with pytest.raises(TokenFetchError, match="session step"):
        await auth._fetch()


async def test_a_failed_token_step_names_the_step(patched_transport):
    patched_transport(handshake_handler(token_status=500))
    auth = session_token_auth(BASE, client_id="id", client_secret="secret")

    with pytest.raises(TokenFetchError, match="token step"):
        await auth._fetch()


async def test_a_token_response_without_a_token_is_rejected(patched_transport):
    patched_transport(handshake_handler(token_body={"expires_in": 900}))
    auth = session_token_auth(BASE, client_id="id", client_secret="secret")

    with pytest.raises(TokenFetchError, match="no access token"):
        await auth._fetch()


async def test_camel_case_token_fields_are_accepted(patched_transport):
    """accessToken and expiresIn are as common as the snake_case spellings."""
    patched_transport(
        handshake_handler(token_body={"accessToken": "tok-2", "expiresIn": 120})
    )
    auth = session_token_auth(BASE, client_id="id", client_secret="secret")

    token, lifetime = await auth._fetch()

    assert token == "tok-2"
    assert lifetime == 120


async def test_a_missing_expiry_falls_back_to_the_default(patched_transport):
    """Treating a token as immortal means every request eventually pays a 401
    and a retry."""
    patched_transport(handshake_handler(token_body={"access_token": "tok-3"}))
    auth = session_token_auth(
        BASE, client_id="id", client_secret="secret", default_lifetime=300.0
    )

    _, lifetime = await auth._fetch()

    assert lifetime == 300.0


async def test_an_unparseable_expiry_falls_back_rather_than_crashing(
    patched_transport,
):
    patched_transport(
        handshake_handler(token_body={"access_token": "t", "expires_in": "soon"})
    )
    auth = session_token_auth(
        BASE, client_id="id", client_secret="secret", default_lifetime=42.0
    )

    _, lifetime = await auth._fetch()

    assert lifetime == 42.0


async def test_a_non_json_token_body_is_rejected(patched_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/session":
            return httpx.Response(
                200, json={}, headers={"set-cookie": "CLSSESSION=x; Path=/"}
            )
        return httpx.Response(200, text="<html>login</html>")

    patched_transport(handler)
    auth = session_token_auth(BASE, client_id="id", client_secret="secret")

    with pytest.raises(TokenFetchError, match="non-JSON"):
        await auth._fetch()


async def test_the_handshake_runs_once_for_concurrent_callers(patched_transport):
    """RefreshingBearerAuth holds a lock, so a burst of requests triggers one
    handshake rather than one per request. That matters more here than for a
    single-call grant, because otherwise the session endpoint gets hit too."""
    import asyncio

    calls: list[str] = []
    patched_transport(handshake_handler(log=calls))
    auth = session_token_auth(BASE, client_id="id", client_secret="secret")

    await asyncio.gather(*(auth.token() for _ in range(10)))

    assert calls.count("/auth/session") == 1
    assert calls.count("/auth/token") == 1
