# service-client

Async clients for the **Accounts** and **Payments** services, built to be
consumed by `async def` FastAPI endpoints sitting behind a gateway.

This repository is a scaffold. The endpoint methods and models are
placeholders to be replaced with the real API's detail; the transport and auth
layers are generic and should not need to change.

## Rename first

`service_client` is a placeholder. Pick the real name before anyone imports it:

```bash
NEW=acme_client
git mv src/service_client src/$NEW
grep -rl service_client . --exclude-dir=.git | xargs sed -i "s/service_client/$NEW/g"
sed -i 's/service-client/acme-client/g' pyproject.toml README.md SETUP.md
make test
```

## Layout

```
src/service_client/
├── transport.py       GENERIC. BaseClient, errors, RetryPolicy. Rarely changes.
├── auth.py            GENERIC. Bearer token refresh.
├── mapping.py         GENERIC. UpstreamContractError, require, maps_upstream.
├── accounts.py        CUSTOMIZE. AccountsClient: accounts, transactions.
├── payments.py        CUSTOMIZE. PaymentsClient: transfers.
└── models/
    ├── accounts.py    GENERATED. Replace with models from real samples.
    └── payments.py    GENERATED. Replace with models from real samples.

tests/
├── conftest.py            Fixtures: MockTransport wiring, fake clock.
├── test_transport.py      GENERIC. Should survive any API change.
├── test_auth.py           GENERIC.
├── test_wiring.py         Shared auth, separate pools, separate policies.
├── test_accounts.py       REWRITE for the real API.
├── test_payments.py       REWRITE. Read test_transfer_is_never_retried first.
└── test_fastapi_app.py    Error mapping from upstream codes onto ours.

examples/
├── fastapi_app.py     Lifespan, dependencies, exception handlers, endpoints.
└── api/               THIS API's contract, separate from the upstream's.
    ├── models.py      Our request and response models.
    ├── mappers.py     Plain functions for unconditional mapping.
    └── transaction_rules.py   Rule table, for the one resource that needs it.
```

`examples/api/` is the layer that keeps a client regeneration from silently
changing your public API. Endpoints never return a client model directly.

The generic/customize split is deliberate: `transport.py` and `auth.py` can
move to another project untouched, without carrying anything proprietary.

## Getting started

Python 3.10 or newer is required.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
make install
make test          # 101 tests should pass before you change anything
```

Useful targets: `make check` for lint, types, and tests; `make verify` to
install into a throwaway venv and confirm `pyproject.toml` declares everything
the tests actually need.

## The two clients

They share a base URL and credentials, so share one auth object. They do
**not** share a retry policy or a connection pool.

```python
from service_client import (
    ACCOUNTS_RETRY, PAYMENTS_RETRY,
    AccountsClient, PaymentsClient, client_credentials_auth,
)

auth = client_credentials_auth(token_url, client_id=..., client_secret=...)

accounts = AccountsClient(base_url, auth=auth, retry=ACCOUNTS_RETRY)
payments = PaymentsClient(base_url, auth=auth, retry=PAYMENTS_RETRY)

account = await accounts.get_account("12345")
async for txn in accounts.iter_transactions("12345"):
    ...
transfer = await payments.create_transfer(request)
```

For a static API key, pass `token=...` instead of `auth=...`. Switching later
changes one line at construction and nothing at any call site.

See `examples/fastapi_app.py` for lifespan wiring, dependency injection, and
mapping upstream errors onto your own status codes.

## Customizing

### 1. Confirm the assumptions

Each client module lists what was guessed at the top. The ones most worth
checking early:

- The query parameters on `list_transactions` (date range, cursor). An
  unrecognized query parameter is usually ignored rather than rejected, so a
  wrong guess fails quietly and returns unfiltered results that look right.
- Whether `/transfers` supports `GET` for listing or read-back by id. A
  money-movement resource you can create but never confirm is unusual, and
  without read-back there is no way to reconcile a create whose response was
  lost.

### 2. Record samples

Models are generated from real responses, **not** from any published spec.
Copy `.env.example` to `.env`, fill it in, then:

```bash
make samples
```

Pick records deliberately across different shapes rather than the first five
IDs. A field null in every sample comes out as `Any` and will silently accept
anything.

Recorded samples are gitignored. They are real payloads; scrub them before
committing anything under `samples/`.

### 3. Generate models

```bash
make models          # or models-accounts / models-payments
```

Check the diff for two things every time: money fields must be `Decimal` and
not `float`, and fields that were nullable must still be nullable.

Never hand-edit the generated modules. The edit is lost on the next
regeneration and nobody notices until a record that exercises it reaches
production.

### 4. Write endpoint methods and tests

One thin method per operation in `accounts.py` or `payments.py`. Then rewrite
`test_accounts.py` and `test_payments.py` so each test pins one fact learned
from a real response, particularly anywhere a spec and the live API disagree.

### 5. Decide your own contract

`examples/api/models.py` is where you choose what your callers see: which
fields, what names, which are required. It is not a copy of the upstream's
shape and should not become one.

For each field the client model treats as optional but your contract requires,
the mapper decides what happens when it is missing. Three options, and the
right one differs per field: declare it optional on your side too, substitute
a documented default, or raise `UpstreamContractError` via `_require`.

## Consuming from another project

```
service-client @ git+ssh://git@github.com/you/service-client.git@v0.1.0
```

Tag every release. Pinning a tag is what lets one project stay on an older
version while another moves ahead.

## Reviews

`REVIEW.md` records a code review of this scaffold: two defects found and
fixed, three maintainability problems, one judgment call. Worth reading before
changing the mapping layer, since two of the findings are subtle failure modes
that look fine in passing tests.

## Design notes

`DECISIONS.md` covers why the clients are shaped this way: the retry budget,
why models come from samples, why Payments never retries, and why upstream
401s become 502s. Read it before changing any of those.
