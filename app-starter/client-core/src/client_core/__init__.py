"""Generic async HTTP client toolkit. Knows nothing about any particular API.

This is the layer you build a new service client on. Subclass BaseClient and
add one thin method per endpoint; you get auth, timeouts, retries with
backoff, status-to-exception mapping and typed response validation for free.

    from client_core import BaseClient, RetryPolicy, client_credentials_auth
    from pydantic import BaseModel

    class Widget(BaseModel):
        id: str

    class WidgetClient(BaseClient):
        async def get_widget(self, widget_id: str) -> Widget:
            return await self._get(f"/widgets/{widget_id}", Widget)

        async def create_widget(self, body: Widget) -> Widget:
            return await self._post("/widgets", Widget, body=body)

    client = WidgetClient(
        base_url,
        auth=client_credentials_auth(token_url, client_id=..., client_secret=...),
        retry=RetryPolicy(max_attempts=4, max_elapsed=20.0),
    )

RETRY POLICY IS A CONSTRUCTOR ARGUMENT, NOT A CONSTANT. Different callers want
different budgets from the same endpoints: a request-driven API races a gateway
timeout, a batch job does not. Define the policies at your own call site, named
for what they are, rather than importing someone else's.

Retries never cover POST unless you set `retry_non_idempotent=True`. Do not set
it on anything that creates a record: a retried POST after a timeout can create
it twice, because the request may have succeeded with only the response lost.

WHAT DOES NOT BELONG HERE: endpoint paths, resource models, domain vocabulary.
If a change to some upstream API would require editing this package, it is in
the wrong place. Resource clients live in their own package on top of this one;
see service-client for a worked example.

TESTING. Pass `http_client` to inject an httpx.MockTransport, and the whole
stack runs with no network:

    BaseClient(base_url, http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=base_url))
"""

from .auth import RefreshingBearerAuth, TokenFetchError, client_credentials_auth
from .mapping import UpstreamContractError, maps_upstream, require
from .transport import (
    APIConnectionError,
    APIError,
    APIStatusError,
    AuthError,
    BaseClient,
    NotFoundError,
    RateLimitError,
    ResponseValidationError,
    RetryPolicy,
    ServerError,
)

__version__ = "0.1.0"

__all__ = [
    "APIConnectionError",
    "APIError",
    "APIStatusError",
    "AuthError",
    "BaseClient",
    "NotFoundError",
    "RateLimitError",
    "RefreshingBearerAuth",
    "ResponseValidationError",
    "RetryPolicy",
    "ServerError",
    "TokenFetchError",
    "UpstreamContractError",
    "client_credentials_auth",
    "maps_upstream",
    "require",
]
