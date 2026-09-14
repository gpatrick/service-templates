"""Two-step auth for the CLS backend: open a session, then exchange it for a token.

CLS does not accept a plain client-credentials grant. It wants:

    1. POST /auth/session   with the credentials      -> Set-Cookie
    2. POST /auth/token     with that cookie attached -> access token

The cookie is the part worth understanding. You never touch it. Both calls go
through ONE httpx.AsyncClient, and httpx keeps a cookie jar per client
instance, so the Set-Cookie from step 1 is sent automatically on step 2. Using
two clients, or httpx.post() twice at module level, silently drops it and step
2 fails with a 401 that looks like bad credentials.

Everything else comes from client_core.RefreshingBearerAuth, which takes any
async fetcher returning (token, lifetime_seconds). That is the whole seam: the
handshake below is just a fetcher with two requests in it instead of one.

What you get for free by using it:

  * proactive refresh before expiry, with leeway
  * one reactive retry on a 401, in case the server expired the token early
  * a lock, so a burst of concurrent requests triggers ONE handshake rather
    than one per request. That matters more here than for a single-call grant,
    since otherwise every caller hits the session endpoint too.
"""

from __future__ import annotations

import httpx
from client_core import RefreshingBearerAuth, TokenFetchError

__all__ = ["session_token_auth", "SESSION_PATH", "TOKEN_PATH"]

# CONFIRM BOTH AGAINST THE REAL API.
SESSION_PATH = "/auth/session"
TOKEN_PATH = "/auth/token"

# The handshake is two round trips, so it needs a longer budget than a single
# token call. Still short: if auth is slow, failing fast beats holding every
# waiting request behind a lock.
HANDSHAKE_TIMEOUT = 20.0


def session_token_auth(
    base_url: str,
    *,
    client_id: str,
    client_secret: str,
    session_path: str = SESSION_PATH,
    token_path: str = TOKEN_PATH,
    leeway: float = 60.0,
    default_lifetime: float = 3600.0,
    verify: bool = True,
) -> RefreshingBearerAuth:
    """Return an httpx.Auth that performs the CLS session-then-token handshake.

        auth = session_token_auth(
            "https://cls.example.com",
            client_id=settings.cls_client_id,
            client_secret=settings.cls_client_secret.get_secret_value(),
        )
        accounts = AccountsClient(base_url, auth=auth, retry=ACCOUNTS_RETRY)

    `base_url` is the auth host. It is often the same host as the API but does
    not have to be, which is why it is a separate argument rather than being
    taken from the client.

    `default_lifetime` applies only when the token response omits an expiry.
    Set it well under whatever the server actually enforces: guessing long
    means every token is used past its death and every request pays a 401 and
    a retry.
    """

    async def fetch() -> tuple[str, float | None]:
        # ONE client for both calls. This is what carries the cookie.
        async with httpx.AsyncClient(
            base_url=base_url, timeout=HANDSHAKE_TIMEOUT, verify=verify
        ) as http:
            session = await _post(
                http,
                session_path,
                {"clientId": client_id, "clientSecret": client_secret},
                step="session",
            )

            # Not fatal by itself: some servers set the cookie on a redirect or
            # use a header instead. But a missing cookie almost always means
            # step 2 is about to fail with a confusing 401, and saying so here
            # points at the real problem.
            if not http.cookies:
                raise TokenFetchError(
                    f"{session_path} returned {session.status_code} but set no "
                    f"cookie; the token call will not be authenticated. Check "
                    f"whether the session is carried by a header instead."
                )

            token = await _post(http, token_path, {}, step="token")

        payload = _json(token, token_path)

        access_token = payload.get("access_token") or payload.get("accessToken")
        if not access_token:
            raise TokenFetchError(
                f"{token_path} returned {token.status_code} with no access "
                f"token field; got keys {sorted(payload)}"
            )

        # expires_in is seconds, and is optional in practice. Falling back to a
        # default is safer than treating the token as immortal.
        raw_lifetime = payload.get("expires_in", payload.get("expiresIn"))
        try:
            lifetime = (
                float(raw_lifetime)
                if raw_lifetime is not None
                else default_lifetime
            )
        except (TypeError, ValueError):
            lifetime = default_lifetime

        return str(access_token), lifetime

    return RefreshingBearerAuth(fetch, leeway=leeway)


async def _post(
    http: httpx.AsyncClient, path: str, body: dict, *, step: str
) -> httpx.Response:
    """POST one step of the handshake, converting every failure to TokenFetchError.

    Both failure modes are folded into one exception type on purpose.
    TokenFetchError is classified as fatal downstream: if the handshake is
    broken, no request will succeed, so a job should stop rather than work
    through its whole failure budget rediscovering that.
    """
    try:
        response = await http.post(path, json=body)
    except httpx.HTTPError as exc:
        raise TokenFetchError(f"{step} step: {path} unreachable: {exc}") from exc

    if response.status_code >= 400:
        # Body included because auth failures are usually explained in it, and
        # without it you are debugging a bare 401.
        raise TokenFetchError(
            f"{step} step: {path} returned {response.status_code}: "
            f"{response.text[:200]}"
        )
    return response


def _json(response: httpx.Response, path: str) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise TokenFetchError(
            f"{path} returned {response.status_code} with a non-JSON body"
        ) from exc
    if not isinstance(payload, dict):
        raise TokenFetchError(f"{path} returned JSON that is not an object")
    return payload
