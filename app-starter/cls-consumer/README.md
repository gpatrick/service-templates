# cls-consumer

A minimal application consuming `cls-client`. Copy the wiring, delete the rest.

## Try it with no backend

    pip install -e ../client-core
    pip install -e ../cls-client
    pip install pydantic-settings

    python demo_mock.py

Prints two lines and exits. No credentials, no `.env`, no server.

`demo_mock.py` gives every `AsyncClient` the app builds an `httpx.MockTransport`,
so the **real** client code runs: the two-step handshake, the cookie,
pagination, validation and error mapping. Only the socket is fake. The mock
token endpoint refuses to issue a token unless the session cookie is present,
so a successful run also proves the handshake carried it.

Delete that file once you point at the real CLS. The same technique belongs in
your test suite; `cls-client/tests/conftest.py` has the fixture.

## Against the real CLS

    cp .env.example .env        # then fill it in
    python app.py

## The wiring, in one function

```python
def build_clients() -> tuple[AccountsClient, PaymentsClient]:
    auth = session_token_auth(
        settings.auth_url,
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
    )
    accounts = AccountsClient(settings.base_url, auth=auth, retry=ACCOUNTS_RETRY)
    payments = PaymentsClient(settings.base_url, auth=auth, retry=PAYMENTS_RETRY)
    return accounts, payments
```

One auth object shared between the two, because Accounts and Payments are one
gateway. **Never share across vendors**: that sends CLS's token somewhere else,
which at best 401s and at worst succeeds where it should not have.

Two retry policies, because reads are safe to repeat and transfers are not.

`auth_url` is a separate setting from `base_url`. They are often the same host,
but the handshake does not have to live where the API does.

## Four things that matter in a consumer

**Build the clients once, not per request.** Each owns a connection pool, and
the auth object holds the cached token. Rebuilding per call throws both away
and re-runs the two-step handshake every time. In FastAPI, construct in
`lifespan` and stash on `app.state`; in a script, at the top of `main`.

**Close them.** `await client.aclose()` in a `finally`, or use `async with`.
Otherwise you leak sockets and get unraisable-exception noise at interpreter
shutdown that looks like a real failure.

**Catch `APIError`.** Everything the client raises is a subclass, so one
`except APIError` catches the lot and nothing leaks a raw httpx or pydantic
exception. Catch `NotFoundError` separately where a 404 is an expected answer
rather than a failure. Catch `TokenFetchError` at startup: if the handshake is
broken, nothing will work, so failing loudly beats discovering it one request
at a time.

**Test with `MockTransport`, not by mocking the client.** Passing a fake client
makes tests pass for the wrong reason; injecting a transport keeps the real
paging, validation and error mapping in play.

## Dependencies

`pyproject.toml` declares `client-core` and `cls-client` as direct references,
because neither is on an index. **Edit the two absolute paths** before running
`pip install .`, or swap them for git URLs once the packages have remotes:

    "cls-client @ git+ssh://git@github.com/you/cls-client@v0.1.0",

Declaring them as bare names instead fails with a confusing "no matching
distribution".

## Files

    app.py          the wiring and a sample call
    settings.py     configuration via pydantic-settings
    demo_mock.py    runs app.py against a fake CLS; delete when real
    .env.example    copy to .env
