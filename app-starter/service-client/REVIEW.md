# Code review

Reviewed at 91 tests passing, ruff and mypy clean, 94% line coverage.

**Status: all findings addressed. Now 101 tests, 97% coverage.** Finding 7 was
added after the original review, when a clean install surfaced a packaging bug
the warm environment had been hiding. Each section
below records the finding and the fix, so the reasoning survives even after
the code stops looking like the problem.

Findings ordered by severity. Three were defects, four maintainability
problems, one a judgment call worth recording either way.

---

## 1. DEFECT: a malformed upstream timestamp becomes a 500

`TransferResponse.created_at` is typed `datetime`. The upstream
`Transfer.created_at` is `str`. Pydantic coerces on construction, so
`to_transfer_response` raises `ValidationError` when the upstream sends
anything that is not ISO-8601:

```python
>>> to_transfer_response(Transfer(transferId="tr-1", createdAt="15/01/2026"))
ValidationError
```

Nothing catches it. `ValidationError` is not in the `APIError` hierarchy and
has no FastAPI exception handler, so it surfaces as an unhandled 500 naming
our own model. That is exactly the failure mode `ResponseValidationError` was
introduced to remove at the transport layer, reintroduced one layer up.

The transport fix was incomplete: it covers parsing the upstream body, not
constructing our own models from it.

**Fixed:** added `maps_upstream` to `service_client.mapping`, applied to every
mapper. A decorator rather than try/except per mapper, because the next mapper
someone adds would otherwise have to remember. Covered by
`test_bad_upstream_timestamp_is_a_contract_violation_not_a_crash`.

---

## 2. DEFECT: `TransactionResponse.kind` is an untyped `str`

`kind` is our own vocabulary, not the upstream's. We control every value it
can take, and there are five. Typing it `str` means a typo in a builder is
accepted silently:

```python
>>> TransactionResponse(transaction_id="t", kind="revrsal")
kind='revrsal'
```

That reaches the caller and becomes a support question. It is also the field
consumers will branch on, so it belongs in the OpenAPI schema as an enum
rather than an open string.

The upstream `type` staying `str` is correct and deliberate, since we cannot
enumerate someone else's values. Ours is the opposite case.

**Fixed:** `TransactionKind` is now a `Literal` in `examples/api/models.py`.
Adding a variant means adding it there first, which is the intended friction:
a new response shape is a contract change.

---

## 3. `client_credentials_auth` has zero test coverage

Lines 134-157 of `auth.py`, the only code in the package that talks to a real
token endpoint, is untested. `RefreshingBearerAuth` is well covered because
the tests inject a fake fetcher, which is good design, but it means the real
fetcher slipped through.

This is the code most likely to be wrong on first contact with the actual API:
form encoding versus JSON body, whether `expires_in` arrives as a string, what
a 401 from the token endpoint looks like. Finding out during a demo is worse
than finding out now.

**Fixed:** five tests at the bottom of `test_auth.py` covering form encoding,
scope, `expires_in` as a string, a 4xx, and a 200 with the wrong body shape.
`auth.py` is now at 100%.

---

## 4. Builders mutate the response after construction

```python
def _build_fee(src):
    out = _base(src, kind="fee")
    out.fee_code = require(...)
    return out
```

Pydantic does not validate on assignment unless `validate_assignment=True`, so
these writes skip the checks that the constructor would apply. It also splits
the definition of a response across two places, which makes the variants
harder to compare when reading.

Low severity today because the assigned values are already the right types.
It is the pattern that is wrong, not the current behavior.

**Fixed:** `_base` now takes variant fields as `**variant` and every builder
is a single constructor call.

---

## 5. Tests import private names from `transaction_rules`

`test_transaction_rules.py` imports `_DEBIT_RULES`, `_FAMILIES`, and friends.

The structural tests they enable are worth having: `test_catch_all_is_last`
catches a class of bug nothing else does, since a rule inserted after the
catch-all leaves every existing test passing while silently disabling the new
variant.

But the underscore says "private" and the test says otherwise. One of them is
lying.

**Fixed:** renamed to `FAMILIES`, `DEBIT_RULES`, `CREDIT_RULES`, `FEE_RULES`
and exported in `__all__`. The rule tables are a legitimate part of that
module's surface for inspection; treating them as private was reflex, not a
decision.

---

## 6. `_put` and `_delete` on `BaseClient` are unused

No endpoint calls either. They are five lines each and cost nothing to keep,
and adding a PUT endpoint later is more pleasant with `_put` already present
and consistent with its siblings.

**Verdict: keep, no change.** Recording it so the next reviewer does not have
to re-derive the reasoning. If they are still unused in six months, delete
them then.

---

## Not findings, checked and sound

- **Retry budget** is bounded by elapsed time, not just attempts, and the
  budget check happens before each sleep rather than after.
- **POST is never retried**, pinned by two tests including one that uses a
  deliberately permissive policy.
- **Money is `Decimal` end to end** and serializes as a JSON string. Scale is
  preserved: `"25.00"` stays `"25.00"`.
- **Auth is shared, pools are not.** Tested in `test_wiring.py`.
- **Upstream status codes never pass through.** 401 becomes 502, and the
  upstream body is not echoed.
- **Error hierarchy is complete** and every member is reachable from
  `service_client`.
- **`examples/api/` is genuinely separate from the client package.** No client
  model appears in a response.
- **The shared mapping surface stayed small.** `UpstreamContractError` plus
  `require`, no framework.

## Coverage gaps, closed

- `TokenFetchError` to 503 and `APIConnectionError` to 504 handlers now have
  tests.
- Unknown transaction type to 502 has an end-to-end test as well as a unit
  one.
- `_apply`'s no-rule-matched raise is still uncovered. It is unreachable while
  every family ends in a catch-all, which `test_catch_all_is_last` enforces. A
  comment now says so rather than leaving the next reader to work it out.

## 7. DEFECT (found after review, during first clean install): fastapi missing from [dev]

`examples/fastapi_app.py` and `tests/test_fastapi_app.py` both import fastapi,
but `pyproject.toml` never declared it. Every check passed during development
because the authoring environment already had fastapi installed for unrelated
reasons. The first person to clone into a fresh venv hit `ModuleNotFoundError`.

`pytest-cov` was missing for the same reason, and would have surfaced the first
time anyone ran coverage.

This is the class of bug a warm environment cannot catch, only a cold one.

**Fixed:** added `fastapi>=0.110` and `pytest-cov>=5.0` to `[dev]`, and added a
`make verify` target that installs into a throwaway venv from `pyproject.toml`
alone and runs the tests there. Run it after touching dependencies and before
tagging a release.

Also added an interpreter version guard to `make install`, since the same
clean-machine path produced a confusing pip error on Python 3.9 rather than a
clear "3.10+ required".

---

## 8. Warnings were being ignored by default

Test runs ended with a warnings summary nobody reads. That is the state in
which a real deprecation, from pydantic or httpx rather than from a test-only
dependency, scrolls past unnoticed until it becomes a hard break on an
upgrade.

**Fixed:** `filterwarnings = ["error", ...]` with two message-scoped
exemptions for starlette's import-time warnings. Verified that an unlisted
DeprecationWarning now fails the run.

Note the exemptions match on message, not category or module. The starlette
warning is a `UserWarning` subclass rather than a `DeprecationWarning`, and
both warnings fire at import time where the reported module is the importer
rather than the emitter, so category and module scoping both miss them.

---

## Remaining uncovered lines, all deliberate

- `transaction_rules.py:148` — the unreachable defence-in-depth raise above.
- `fastapi_app.py:119,123` — the two dependency functions, which only execute
  when lifespan's real clients are used. Every test overrides them, which is
  the point of the seam.
- `transport.py` — `_put`, `_delete`, and branches of `_encode` that no
  current endpoint exercises. See finding 6.
