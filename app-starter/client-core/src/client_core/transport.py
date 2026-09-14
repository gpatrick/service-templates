"""Generic async HTTP transport. Nothing in this module is API-specific.

BaseClient owns connection lifecycle, retries, error mapping, and response
validation. Subclass it in endpoints.py and add one thin method per
operation; see that module for the pattern.

This file is the reusable half of the package. It should not need to change
when the upstream API does.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Mapping
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

M = TypeVar("M", bound=BaseModel)

__all__ = [
    "BaseClient",
    "RetryPolicy",
    "APIError",
    "APIConnectionError",
    "APIStatusError",
    "ResponseValidationError",
    "AuthError",
    "NotFoundError",
    "RateLimitError",
    "ServerError",
    "raise_for_status",
]


# --------------------------------------------------------------------------
# Errors
#
# The full hierarchy is defined even though some subclasses are not raised on
# every code path. Adding a subclass later is backward compatible; removing
# one breaks every caller that catches it.
# --------------------------------------------------------------------------


class APIError(Exception):
    """Base class for everything this client raises."""


class APIConnectionError(APIError):
    """Network failure or timeout. No response was received."""


class ResponseValidationError(APIError):
    """A 2xx response body did not match the model we expected.

    Raised instead of letting pydantic's ValidationError escape, for two
    reasons. Callers catch APIError and its subclasses; a bare ValidationError
    slips through that net and surfaces as a 500 blaming us for the upstream's
    payload. And the raw body belongs in the message, since the validation
    error alone rarely says enough to diagnose a real response.

    Usually means the models need regenerating from fresh samples: the API
    changed, or the recorded corpus missed a record shape.
    """

    def __init__(self, model: str, response: httpx.Response, cause: Exception) -> None:
        self.model = model
        self.body = response.content
        self.url = str(response.request.url)
        self.request_id = response.headers.get("x-request-id")

        detail = self.body[:300].decode("utf-8", errors="replace")
        super().__init__(
            f"response from {self.url} did not match {model}: {cause}; body was {detail}"
        )


class APIStatusError(APIError):
    """The server responded with 4xx or 5xx."""

    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        self.body = response.content
        self.url = str(response.request.url)
        self.request_id = response.headers.get("x-request-id")

        detail = self.body[:300].decode("utf-8", errors="replace")
        suffix = f" (request-id {self.request_id})" if self.request_id else ""
        super().__init__(f"{self.status_code} from {self.url}{suffix}: {detail}")


class AuthError(APIStatusError):
    """401 or 403."""


class NotFoundError(APIStatusError):
    """404."""


class RateLimitError(APIStatusError):
    """429, after retries were exhausted."""


class ServerError(APIStatusError):
    """5xx, after retries were exhausted."""


_STATUS_MAP: dict[int, type[APIStatusError]] = {
    401: AuthError,
    403: AuthError,
    404: NotFoundError,
    429: RateLimitError,
}


def raise_for_status(response: httpx.Response) -> None:
    """Map a non-2xx response onto the exception hierarchy."""
    if response.status_code < 400:
        return
    cls = _STATUS_MAP.get(response.status_code)
    if cls is None:
        cls = ServerError if response.status_code >= 500 else APIStatusError
    raise cls(response)


# --------------------------------------------------------------------------
# Retries
# --------------------------------------------------------------------------

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RetryPolicy:
    """Exponential backoff with jitter, bounded by a total elapsed budget.

    max_elapsed matters most when this client sits behind a gateway. Without
    it, several attempts plus backoff can run past any sane gateway limit,
    and the caller is long gone by the time the last attempt returns. Set it
    below whatever the gateway allows.
    """

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        max_elapsed: float = 20.0,
        base_delay: float = 0.25,
        max_delay: float = 5.0,
        retry_non_idempotent: bool = False,
    ) -> None:
        self.max_attempts = max_attempts
        self.max_elapsed = max_elapsed
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.retry_non_idempotent = retry_non_idempotent

    def should_retry(self, method: str, status: int | None, attempt: int) -> bool:
        if attempt >= self.max_attempts:
            return False
        if not self.retry_non_idempotent and method.upper() not in IDEMPOTENT_METHODS:
            # Retrying a POST after a timeout can duplicate a create. Enable
            # this only for endpoints that accept an idempotency key.
            return False
        if status is None:
            return True  # connection error
        return status in RETRYABLE_STATUS

    def delay(self, attempt: int, response: httpx.Response | None = None) -> float:
        hint = self._retry_after(response)
        if hint is not None:
            return min(hint, self.max_delay)
        # 2.0 rather than 2: int ** int is typed as Any, since a negative
        # exponent would produce a float.
        backoff = min(self.base_delay * (2.0 ** (attempt - 1)), self.max_delay)
        # Full jitter: without it, every rate-limited client retries in
        # lockstep and recreates the burst that caused the 429.
        return backoff * (0.5 + random.random() * 0.5)

    @staticmethod
    def _retry_after(response: httpx.Response | None) -> float | None:
        if response is None:
            return None
        value = response.headers.get("retry-after")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None  # HTTP-date form; fall back to backoff


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class BaseClient:
    """Async HTTP client with retries, error mapping, and typed responses.

    Construct once per process, in the FastAPI lifespan handler, and share it
    across requests. One client per request throws away connection pooling
    and refetches a token on every call.

    Pass http_client to supply your own transport; that is the seam the tests
    use to inject a MockTransport.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        auth: httpx.Auth | None = None,
        timeout: float = 10.0,
        retry: RetryPolicy | None = None,
        max_connections: int = 50,
        user_agent: str = "service-client/0.1",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._retry = retry or RetryPolicy()

        if http_client is not None:
            self._client = http_client
            self._owns_client = False
            return

        if (token is None) == (auth is None):
            raise ValueError("pass exactly one of token or auth")

        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        if token is not None:
            # A static token needs no refresh machinery. For expiring tokens
            # pass an httpx.Auth as `auth`; no call site has to change.
            headers["Authorization"] = f"Bearer {token}"

        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            auth=auth,
            headers=headers,
            # Bound the pool. Unbounded, a slow upstream lets in-flight
            # requests pile up until the process runs out of descriptors.
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max(1, max_connections // 2),
            ),
            # Transport retries cover connection failures only; they cannot
            # see a status code. Status retries happen in _request.
            transport=httpx.AsyncHTTPTransport(retries=2),
            follow_redirects=True,
        )
        self._owns_client = True

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- plumbing ----------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        # Drop unset optional arguments so they do not serialize as "None".
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        deadline = time.monotonic() + self._retry.max_elapsed

        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                response = await self._client.request(
                    method, path, params=clean, json=json, headers=headers
                )
            except httpx.TransportError as exc:
                # TimeoutException subclasses TransportError; this covers both.
                if not self._retry.should_retry(method, None, attempt):
                    raise APIConnectionError(f"{method} {path}: {exc}") from exc
                delay = self._retry.delay(attempt)
                if time.monotonic() + delay > deadline:
                    raise APIConnectionError(
                        f"{method} {path}: retry budget exhausted: {exc}"
                    ) from exc
                await asyncio.sleep(delay)
                continue

            if response.status_code < 400:
                return response

            if not self._retry.should_retry(method, response.status_code, attempt):
                raise_for_status(response)

            delay = self._retry.delay(attempt, response)
            if time.monotonic() + delay > deadline:
                # Out of budget. Surface the last response rather than
                # sleeping into a gateway timeout nobody is waiting on.
                raise_for_status(response)
            await asyncio.sleep(delay)

        raise APIError(f"{method} {path}: retries exhausted")

    @staticmethod
    def _validate(model: type[M], response: httpx.Response) -> M:
        """Parse a 2xx body, converting pydantic errors into our hierarchy."""
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            raise ResponseValidationError(model.__name__, response, exc) from exc

    async def _get(self, path: str, model: type[M], **params: Any) -> M:
        response = await self._request("GET", path, params=params)
        return self._validate(model, response)

    async def _post(
        self,
        path: str,
        model: type[M],
        *,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        **params: Any,
    ) -> M:
        """POST, optionally with per-request headers.

        `headers` exists so a caller can send an idempotency key, which is
        per-request by nature and therefore cannot live in the client's
        constructor headers. It is a named parameter rather than part of
        **params on purpose: **params becomes the query string, so a caller
        who passed headers={...} without this would have the value silently
        serialized as a query parameter and the header never sent. That
        failure is invisible, because an API ignores an unrecognized query
        parameter and returns 200.
        """
        response = await self._request(
            "POST", path, params=params, json=_encode(body), headers=headers
        )
        return self._validate(model, response)

    async def _put(
        self, path: str, model: type[M], *, body: Any = None, **params: Any
    ) -> M:
        response = await self._request("PUT", path, params=params, json=_encode(body))
        return self._validate(model, response)

    async def _delete(self, path: str, **params: Any) -> None:
        await self._request("DELETE", path, params=params)

    async def raw(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Escape hatch for endpoints not yet wrapped in endpoints.py."""
        return await self._request(method, path, **kwargs)


def _encode(body: Any) -> Any:
    if isinstance(body, BaseModel):
        return body.model_dump(mode="json", by_alias=True, exclude_unset=True)
    return body
