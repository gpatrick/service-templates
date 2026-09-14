# client-core

Generic async HTTP client toolkit. Knows nothing about any particular API.

This is the layer you build a new service client on. `service-client` is a
worked example: it depends on this package and adds resource clients for one
specific upstream.

## Building a client

Subclass `BaseClient`, add one thin method per endpoint. You get auth,
timeouts, retries with backoff, status-to-exception mapping and typed response
validation without writing any of it.

```python
from pydantic import BaseModel, ConfigDict, Field
from client_core import BaseClient, RetryPolicy, client_credentials_auth


class Widget(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    widget_id: str = Field(alias="widgetId")
    name: str | None = None


class WidgetClient(BaseClient):
    async def get_widget(self, widget_id: str) -> Widget:
        return await self._get(f"/widgets/{widget_id}", Widget)

    async def list_widgets(self, *, cursor: str | None = None) -> WidgetPage:
        # None params are dropped, so an unset cursor is simply not sent.
        return await self._get("/widgets", WidgetPage, cursor=cursor)

    async def create_widget(self, body: Widget) -> Widget:
        return await self._post("/widgets", Widget, body=body)


client = WidgetClient(
    "https://api.example.com",
    auth=client_credentials_auth(token_url, client_id=..., client_secret=...),
    retry=RetryPolicy(max_attempts=4, max_elapsed=20.0),
    timeout=15.0,
)
```

`_post` takes a plain dict as well as a pydantic model, so a payload you were
handed as JSON can go straight through.

## Retry policy belongs at your call site

`RetryPolicy` is a constructor argument, not a module constant, because
different callers want different budgets from the same endpoints. A
request-driven API races a gateway timeout; a scheduled job does not. Define
the policies in your own code, named for what they are:

```python
# Reads are idempotent and cheap to repeat.
READ_RETRY = RetryPolicy(max_attempts=4, max_elapsed=20.0)

# Writes get one attempt.
WRITE_RETRY = RetryPolicy(max_attempts=2, max_elapsed=30.0)
```

**Retries never cover POST unless you set `retry_non_idempotent=True`.** Do not
set it on anything that creates a record. A retried POST after a timeout can
create it twice, because the request may have succeeded with only the response
lost. If you need safe write retries, send an idempotency key and confirm the
upstream honours it first.

`max_elapsed` matters most behind a gateway. Without it, several attempts plus
backoff can outlast any sane gateway limit, and the caller is long gone by the
time the last attempt returns.

## Errors

Every failure arrives as an `APIError` subclass, so one `except APIError`
catches the lot and nothing leaks a raw `httpx` or `pydantic` exception.

    APIError                   base
      APIConnectionError       no response: DNS, refused, timeout
      APIStatusError           a non-2xx response
        AuthError              401, 403
        NotFoundError          404
        RateLimitError         429
        ServerError            5xx
      ResponseValidationError  2xx whose body does not match the model

    TokenFetchError            the token endpoint refused or misbehaved
    UpstreamContractError      a 2xx your application cannot represent

The split that matters operationally is `APIStatusError` with a 4xx, meaning
the request is wrong and will be wrong the same way next time, against
everything else, which is worth retrying later.

## Auth

`client_credentials_auth(token_url, client_id=..., client_secret=...)` returns
an `httpx.Auth` that fetches a token, caches it, refreshes before expiry, and
retries once on a 401. Concurrent callers share one refresh rather than
stampeding the token endpoint.

For a static token, pass `token=` instead. Pass exactly one of `token` or
`auth`; passing both or neither raises.

**One auth object per upstream.** Two clients against the same gateway can
share one. Two clients against different systems must not: sharing would send
one system's token to the other.

## Testing

Pass `http_client` to inject a transport, and the whole stack runs with no
network: same paging, same validation, same error mapping.

```python
def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"widgetId": "1"})

client = WidgetClient(
    "https://api.example.com",
    http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.example.com",
    ),
)
```

A client only closes transports it created, so an injected one is yours to
close.

`tests/conftest.py` has a `DemoClient` doing exactly this, if you want a
worked example to copy.

## Install

Not published to an index. Install from a local path:

    pip install -e ../client-core

## What does not belong here

Endpoint paths, resource models, domain vocabulary. If a change to some
upstream API would require editing this package, it is in the wrong place.

That rule is enforced: `service-client`'s test suite asserts that
`AccountsClient`, `PaymentsClient` and the retry constants named after them are
*not* importable from `client_core`.
