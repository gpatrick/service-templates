# cls-client

Typed async clients for the CLS backend. One vendor package on top of
`client-core`.

`client_core` owns transport, retries, auth machinery, error mapping and
response validation. Everything here is CLS-specific: its endpoints, its
models, its two-step auth handshake, and retry budgets tuned to it.

## Install

    pip install -e ../client-core
    pip install -e .

## Use

```python
from cls_client import ACCOUNTS_RETRY, PAYMENTS_RETRY, AccountsClient, \
    PaymentsClient, session_token_auth

auth = session_token_auth(
    "https://cls.example.com",
    client_id=settings.cls_client_id,
    client_secret=settings.cls_client_secret.get_secret_value(),
)

async with AccountsClient(base_url, auth=auth, retry=ACCOUNTS_RETRY) as accounts:
    account = await accounts.get_account("12345")
    async for txn in accounts.iter_transactions("12345", start_date="2026-03-01"):
        ...
```

One auth object shared between Accounts and Payments, since they are one
gateway. **Never share across vendors**: that sends CLS's token somewhere else,
which at best 401s and at worst succeeds where it should not have.

Two retry policies, because reads are safe to repeat and transfers are not.

## Endpoints

    AccountsClient.get_account(account_id)
    AccountsClient.list_transactions(account_id, start_date=, end_date=,
                                     limit=, cursor=)
    AccountsClient.iter_transactions(account_id, start_date=, end_date=,
                                     page_size=)      # follows the cursor
    PaymentsClient.create_transfer(TransferRequest)

Same surface as the original service client, so code written against that
moves over with only the import changed.

## Auth: session, then token

CLS does not accept a plain client-credentials grant:

    1. POST /auth/session   with the credentials      -> Set-Cookie
    2. POST /auth/token     with that cookie attached -> access token

**You never touch the cookie.** Both calls go through one `httpx.AsyncClient`,
and httpx keeps a cookie jar per client instance, so the `Set-Cookie` from step
1 is sent automatically on step 2.

That is the one thing to preserve if you edit `auth.py`. Splitting the fetcher
into two clients, or calling `httpx.post()` twice, silently drops the cookie
and step 2 fails with a 401 that reads as bad credentials. `auth.py` raises a
named error if the session sets no cookie, precisely so that failure points at
the right place. `tests/test_auth.py` has a mock server that refuses to issue a
token without the cookie, which is what keeps the behaviour honest.

Everything else comes from `client_core.RefreshingBearerAuth`, which takes any
async fetcher returning `(token, lifetime_seconds)`. The handshake is just a
fetcher with two requests in it. You get proactive refresh before expiry, one
reactive retry on a 401, and a lock so a burst of concurrent requests triggers
one handshake rather than one per request. That last part matters more here
than for a single-call grant, because otherwise the session endpoint gets hit
just as hard.

### To confirm against the real API

- `SESSION_PATH` and `TOKEN_PATH` in `auth.py`. Both are currently guesses.
- The request bodies. Currently `{"clientId": ..., "clientSecret": ...}` for
  the session and `{}` for the token.
- Whether the token response reports a lifetime. If it does not,
  `default_lifetime` applies; set it well under whatever the server enforces,
  because guessing long means every token is used past its death and every
  request pays a 401 and a retry.
- Whether the session cookie expires sooner than the token. If it does, the
  proactive refresh will not save you and you are relying on the 401 path.

## Models

`src/cls_client/models/` is carried over from the original client and is
**placeholder**. Regenerate from recorded CLS responses before trusting it.
Check every regeneration diff for money fields: generators read a JSON number
as `float`, and binary floating point cannot represent most decimal amounts
exactly.

## Tests

    pytest -q        # 18: 11 auth, 7 endpoints

Everything runs through `httpx.MockTransport`, so no network. The auth tests
patch the transport rather than the client, keeping the real cookie jar in
play; a fake client would make them pass for the wrong reason.
