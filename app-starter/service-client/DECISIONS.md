# Design decisions

Why the client is shaped this way. Read this before changing any of it; each
of these has a failure mode behind it.

## Models are generated from recorded responses, not from a published spec

The evaluation that produced this client started from a vendor OpenAPI
document that turned out not to describe the API. It disagreed with live
behavior on:

- The auth scheme. It declared `type: apiKey` with `name: Bearer` and
  `in: header`, which literally means a header named `Bearer`. The API
  expects the standard `Authorization: Bearer <token>`. Swagger 2.0 had no
  bearer type, so this was the usual workaround written incorrectly.
- The scheme was defined but never referenced, so no operation required auth.
- Six path templates collided after normalization, including one pair that
  differed only by a required query parameter.
- Fields marked required came back null.
- A large share of declared examples did not validate against their schemas.

Fixing the spec would have meant discovering the truth field by field from
live responses anyway, then paying a second time to encode it back. Sampling
directly is the same work once.

**Consequence:** `models.py` is a build artifact. Hand edits are lost on the
next `make models` and the loss is silent. Corrections belong in the sample
corpus or in the intermediate JSON Schema.

**Risk to manage:** sample-derived models encode only what the recorded
records contained. A field null across every sample becomes `Any` and accepts
anything. Choose samples across different record shapes, not the first five
IDs.

## Two clients, one auth object, separate pools

Accounts and Payments share a base URL and credentials but not a risk profile.

**Shared:** one `RefreshingBearerAuth` instance, so one token cache and one
refresh serve both. The lock inside it makes concurrent access from two
clients safe. A 401 from either service invalidates the token for both, which
is correct when it really is one credential.

**Not shared:** retry policy and connection pool.

`ACCOUNTS_RETRY` is more permissive because reads are idempotent and cheap to
repeat. `PAYMENTS_RETRY` is conservative because everything there moves money.

Separate pools are a bulkhead. With one shared pool, a burst of Accounts
traffic can consume every connection and queue transfers behind it. That is a
read-heavy load spike stalling money movement, which is the wrong failure.
The cost is some idle sockets. `max_connections` is split explicitly in
`examples/fastapi_app.py` (40 and 10) rather than left at the default 50 each,
which would open 100 against one host.

**If the two services ever diverge in release cadence,** split into three
packages with a shared core. The current layout makes that mechanical.

## Retries are bounded by elapsed time, not just attempt count

The call path is gateway to this service to the upstream API. Attempt count
alone lets several attempts plus backoff run past the gateway's limit, so the
last attempt returns to a caller who left long ago. Under load that becomes
pileup.

`RetryPolicy.max_elapsed` is checked before every sleep. Defaults are 3
attempts, 10s per-request timeout, 20s budget, sized for a 30s gateway limit.
**Re-check these if the gateway timeout changes.**

## POST is never retried, and on Payments this is load-bearing

A retried POST after a timeout can create a duplicate, because the request may
have succeeded with only the response lost. On `create_transfer` that means
moving money twice.

`retry_non_idempotent=False` is the default and must stay that way on
`PaymentsClient`. Note that raising `max_attempts` alone does nothing for
POST; both flags would have to change. `test_transfer_is_never_retried` and
`test_transfer_not_retried_even_with_a_permissive_policy` pin this.

If the API gains an idempotency key, the correct change is to send the key and
reuse it across retries, not to enable blind retrying.

## Money is Decimal, never float

Binary floating point cannot represent most decimal amounts exactly, and the
error compounds across a summed transaction list. `datamodel-codegen` infers
`float` from a JSON number, so **check every regeneration diff** for money
fields that came back as `float`.

If the API sends amounts as JSON numbers, also check whether they are minor
units (an integer count of cents) before accepting the inferred type.

## Request and response models are separate

`TransferRequest` has no id, status, or timestamps; `Transfer` does. Merging
them would force those fields optional and lose the check that a caller
supplied everything required at construction time rather than at the API.

## Backoff uses full jitter

Without jitter, every client rate-limited at the same moment retries at the
same moment and recreates the burst that caused the 429.

## Two layers of retry, on purpose

`httpx.AsyncHTTPTransport(retries=2)` handles connection-level failures. It
cannot see a status code, so 429 and 5xx are handled in `_request`. Removing
either leaves a gap.

## The full exception hierarchy is defined even where unused

`RateLimitError` and `ServerError` are not raised on every path. They exist
because adding a subclass later is backward compatible and removing one
breaks every caller that catches it. Two projects consume this.

## `TokenFetchError` is not an `APIError`

Failing to obtain a token is a configuration problem on our side. Failing to
call the API is an upstream problem. The FastAPI layer maps them to different
status codes (503 vs 502), which is only possible if they are distinguishable.

## Upstream status codes do not pass through

A 401 between us and the vendor is not a 401 between the gateway and us;
forwarding it tells our caller to re-authenticate with the wrong system.
`examples/fastapi_app.py` maps 404 to 404, 429 to 429, and everything else
to 502. Upstream response bodies are not echoed either, since they may leak
internals.

## Token refresh has proactive and reactive triggers

Proactive: refresh within `leeway` seconds of stated expiry, so a token
cannot expire mid-flight. Reactive: on a 401, discard and retry once, which
covers revocation ahead of stated expiry.

`invalidate(stale=...)` takes the token that was rejected. Under concurrency
several requests can be in flight on a revoked token; without the check, each
401 would discard whatever is cached including a replacement another task
already fetched, producing a refresh storm.

## The client is async

It is called from `async def` FastAPI endpoints. A sync `httpx.Client` there
blocks the event loop for the whole upstream round trip. If you ever need a
sync variant, write it separately rather than trying to share an
implementation; every scheme for unifying the two is worse than the
duplication.

## One client per process, not per request

Constructed in the FastAPI lifespan handler and shared. A client per request
discards connection pooling and refetches a token every call. The pool is
bounded (`max_connections=50`) so a slow upstream cannot accumulate in-flight
requests until the process runs out of descriptors.

## `http_client` injection exists for testing

Every test wires a `MockTransport` through this parameter rather than patching
internals, so refactoring does not break the suite. If a test needs
`mock.patch` on an underscored attribute, the seam is in the wrong place.

## Warnings are errors, with a short exemption list

`filterwarnings = ["error", ...]` in `pyproject.toml`. A new deprecation from
httpx, pydantic, or fastapi fails the test run the day it appears rather than
the day it becomes a removal, which is the cheaper day to handle it.

The exemptions are therefore an inventory of what we are deferring, and the
list should shrink over time. Two entries today, both from starlette's
TestClient at import time and neither actionable from here.

Each ignore matches a specific message rather than a category or module.
A blanket `ignore::DeprecationWarning` would hide the warnings we want, and
module scoping does not work for these two: they fire at import time, where
the reported module is the importer rather than the emitter. The starlette one
is also a `UserWarning` subclass rather than a `DeprecationWarning`, so
category matching alone misses it.

## The shared mapping surface is deliberately tiny

`service_client/mapping.py` holds `UpstreamContractError`, `require`, and
`maps_upstream`. That is all that travels between applications.

Response models and mappers are per-application contracts and do not travel.
There is no `Mapper` base class, registry, or dispatch framework, because two
of the three consuming applications map unconditionally and a plain function
is the right shape for that. Building the framework would make those two carry
machinery for a case they do not have.

The error type lives in the client package rather than an application because
the shared FastAPI handlers need to import it in order to catch it.

## Conditional mapping is a rule table, and only where it is needed

Transactions dispatch on the upstream `type` and then on field conditions;
accounts and transfers do not. So `transaction_rules.py` exists and the other
two stay plain functions in `mappers.py`.

Two stages kept separate: a dict on the tag picks a family, an ordered rule
list refines within it. Flattening these into one predicate list keyed on
tag-plus-conditions is the version that becomes unmaintainable, because every
rule then re-checks the tag and the families stop being visible.

First match wins, which suits mutually exclusive outcomes. **If two conditions
ever need to BOTH affect one response, switch to a base build plus applied
modifiers rather than adding flags to rules.** Retrofitting first-match rules
to accumulate is a rewrite of every rule, because each assumes it owns the
whole output.

Nothing defaults silently. An unmatched payload or unknown tag raises. The
rule tables themselves are tested: `test_catch_all_is_last` catches a rule
inserted after the catch-all, which would leave every existing test passing
while silently disabling the new variant.

If a second application later needs conditional mapping, copy the `Rule`
dataclass rather than extracting a shared one. Two copies of fifteen lines is
cheaper than a shared abstraction serving two slightly different dispatch
needs. At three, extract it, and by then the right shape will be obvious.

## Mappers are wrapped in `maps_upstream`

A mapper builds an application model from upstream values, so a type the
upstream got wrong raises `ValidationError` naming OUR model. That sits
outside the `APIError` hierarchy and outside every exception handler, landing
as an unhandled 500 that blames this service for someone else's payload.

`ResponseValidationError` fixed this at the transport layer for parsing the
upstream body. `maps_upstream` fixes the same class of bug one layer up, for
constructing our models from it. A decorator rather than try/except per
mapper, so the next mapper added cannot forget.

## Our vocabulary is a Literal; theirs is a str

`TransactionResponse.kind` is `Literal[...]`, because we control every value
and a typo in a builder should fail at construction rather than reach a
caller. It also publishes as an enum in the OpenAPI schema, and `kind` is
what consumers branch on.

The upstream `Transaction.type` stays a plain `str`, because we cannot
enumerate someone else's values and a `Literal` would turn an unknown type
into a parse failure for the whole page rather than one row.

## Endpoints never return a client model

Each FastAPI endpoint maps the client's model onto one of ours, defined in
`examples/api/models.py`. The mapping functions live in
`examples/api/mappers.py`.

Without this layer, `make models` would silently change this API's public
contract: an upstream field rename would flow straight through to callers. With
it, the same rename breaks a mapper at a known location, and the mapper tests
say which field.

It also lets our contract differ from theirs. We expose snake_case, drop
`accountId` from transaction rows because the caller supplied it in the path,
and declare fields required that the client model treats as optional.

Mappers are pure functions, so their tests need no transport and no event
loop. That is deliberate: they are cheap enough to write one per field, and
they are where an upstream change should surface.

## Money crosses our API boundary as a JSON string

`Decimal` serialized via `PlainSerializer(str)`. A JSON number invites
consumers to parse it as a float, which reintroduces the precision problem one
layer further out where we cannot see it. `str(Decimal("25.00"))` is `"25.00"`,
so the scale the value carries is preserved in transit.

## Pagination is exposed, not drained

`GET /accounts/{id}/transactions` returns a page plus a cursor rather than
looping `iter_transactions` server-side. Draining would turn an unbounded
upstream result into an unbounded response and an unbounded memory footprint
per request. `iter_transactions` stays available for batch work where that is
the right trade.

## A malformed 2xx body is 502, not 500

`BaseClient._validate` converts pydantic's `ValidationError` into
`ResponseValidationError`, which subclasses `APIError`. Two reasons: callers
catch `APIError` and a bare `ValidationError` slips through that net, and the
raw body belongs in the message because the validation error alone rarely says
enough to diagnose a real response.

Left unconverted, an upstream that starts omitting a field lands as an
unhandled 500, pointing the on-call engineer at our service rather than at a
stale model. It almost always means the models need regenerating.

## Endpoint methods stay one line

All behavior lives in `BaseClient._request`. An endpoint that does its own
error handling or validation is a bug waiting to happen, because the next one
added will forget.

## Pagination lives in the client, not at call sites

`AccountsClient.iter_transactions` follows the cursor so two consuming
projects do not each write their own paging loop and get it subtly different.
A test pins that filters are passed to every page, not just the first, which
is the usual way a hand-written loop goes wrong.

## Models are split per service

`models/accounts.py` and `models/payments.py` rather than one module. The two
services version independently, and a shared models file becomes a merge point
for changes that have nothing to do with each other.
