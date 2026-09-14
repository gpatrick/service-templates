"""Async bearer auth with automatic token refresh.

Plugs into the client via the `auth=` parameter:

    auth = client_credentials_auth(
        "https://api.example.com/oauth/token",
        client_id=os.environ["CLIENT_ID"],
        client_secret=os.environ["CLIENT_SECRET"],
    )
    async with ServiceClient(base_url, auth=auth) as api:
        thing = await api.get_thing("12345")

Nothing in the client or at any call site changes when you switch from a
static token to this.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx

__all__ = ["RefreshingBearerAuth", "client_credentials_auth", "TokenFetchError"]

# An async callable returning (token, lifetime_seconds). Lifetime may be None
# when the server does not say, in which case DEFAULT_LIFETIME is assumed.
TokenFetcher = Callable[[], Awaitable["tuple[str, float | None]"]]

DEFAULT_LIFETIME = 3600.0


class TokenFetchError(Exception):
    """Could not obtain a token.

    Deliberately not an APIError. Failing to get a token is a different
    problem from failing to call the API, and the FastAPI layer maps them to
    different status codes.
    """


class RefreshingBearerAuth(httpx.Auth):
    """Attaches a bearer token, refreshing it before expiry.

    Two triggers for a refresh:

      1. Proactive. The cached token is within `leeway` seconds of its stated
         expiry, so it is replaced before it can fail mid-flight.
      2. Reactive. The server returned 401 anyway, which happens when a token
         is revoked ahead of its stated expiry. The token is discarded and the
         request retried exactly once.

    One instance is shared by every request the client makes, so token access
    is guarded by an asyncio.Lock. Without it, a burst of concurrent requests
    on an expired token would each fire their own refresh.
    """

    def __init__(self, fetch: TokenFetcher, *, leeway: float = 60.0) -> None:
        self._fetch = fetch
        self._leeway = leeway
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    # -- public ------------------------------------------------------------

    async def token(self) -> str:
        async with self._lock:
            if self._needs_refresh():
                await self._refresh_locked()
            assert self._token is not None
            return self._token

    async def invalidate(self, stale: str | None = None) -> None:
        """Discard the cached token.

        Pass the token you just saw rejected. If another task has already
        refreshed since then, the newer token is kept rather than thrown
        away, which stops a burst of 401s from causing a refresh storm.
        """
        async with self._lock:
            if stale is not None and self._token != stale:
                return
            self._token = None
            self._expires_at = 0.0

    # -- httpx hook --------------------------------------------------------

    async def async_auth_flow(self, request: httpx.Request):
        token = await self.token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request

        if response.status_code == 401:
            await self.invalidate(stale=token)
            request.headers["Authorization"] = f"Bearer {await self.token()}"
            yield request

    # -- internal ----------------------------------------------------------

    def _needs_refresh(self) -> bool:
        if self._token is None:
            return True
        # monotonic, not time.time: an NTP step must not make a valid token
        # look expired or an expired one look valid.
        return time.monotonic() >= self._expires_at - self._leeway

    async def _refresh_locked(self) -> None:
        try:
            token, lifetime = await self._fetch()
        except httpx.HTTPError as exc:
            raise TokenFetchError(f"token request failed: {exc}") from exc

        if not token:
            raise TokenFetchError("token endpoint returned an empty token")

        self._token = token
        self._expires_at = time.monotonic() + (lifetime or DEFAULT_LIFETIME)


def client_credentials_auth(
    token_url: str,
    *,
    client_id: str,
    client_secret: str,
    scope: str | None = None,
    timeout: float = 10.0,
    leeway: float = 60.0,
) -> RefreshingBearerAuth:
    """OAuth2 client credentials grant."""

    async def fetch() -> tuple[str, float | None]:
        data = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if scope:
            data["scope"] = scope

        # A short-lived client on purpose: routing this through the
        # authenticated client would need a token to fetch a token.
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(token_url, data=data)

        if response.status_code >= 400:
            detail = response.content[:200].decode("utf-8", errors="replace")
            raise TokenFetchError(f"{response.status_code} from {token_url}: {detail}")

        payload = response.json()
        if "access_token" not in payload:
            raise TokenFetchError(f"no access_token in response from {token_url}")

        # expires_in is optional in the OAuth spec; some servers omit it.
        lifetime = payload.get("expires_in")
        return payload["access_token"], float(lifetime) if lifetime else None

    return RefreshingBearerAuth(fetch, leeway=leeway)
