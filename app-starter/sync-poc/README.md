# sync-poc

One file. Reads every page of the source, picks a destination model per
transaction, builds it, posts it.

    pip install -e ../client-core

    export SRC_URL=... SRC_TOKEN_URL=... SRC_CLIENT_ID=... SRC_CLIENT_SECRET=...
    export DST_URL=... DST_TOKEN_URL=... DST_CLIENT_ID=... DST_CLIENT_SECRET=...

    python sync_poc.py            # dry run: print the bodies
    python sync_poc.py --post     # actually post
    python sync_poc.py --limit 5

Dry run is the default.

## What to change

Six `CHANGE ME` sections in `sync_poc.py`, in the order you would work through
them.

1. **CONFIG** — paths and retry policies. The URLs and credentials come from
   the environment and are required, so a missing one fails at import.
2. **SOURCE MODELS** — drop in your page envelope and record model. If the
   endpoint returns a bare array rather than an envelope, the `RootModel` form
   is in the docstring.
3. **DESTINATION MODELS** — one per destination transaction type.
4. **SOURCE CLIENT** — the GET. One method on a `BaseClient` subclass.
5. **DESTINATION CLIENT** — the POST.
6. **SELECTION** — `choose()` returns a type name or `None`; `build()`
   constructs that type's model.

Section 7 is the runner. Leave it alone.

## Two things that will bite

**Keep `transaction_type` required, not defaulted.** `BaseClient` encodes with
`exclude_unset=True`, so a field that is only a class default never gets sent.
A discriminator declared as `transaction_type: str = "FEE"` would silently
vanish from the wire and the destination would receive a body with no type.
Required means a builder that forgets it fails at construction instead.

**Sign conventions are per type.** `build` sends `abs()` for `REFUND` and the
raw amount for the others, on the assumption the destination takes direction
from the type. Confirm that per type before posting: getting it backwards posts
cleanly and reconciles wrongly, weeks later, in someone else's report.

## Reading the output

Per transaction: the source record, the type chosen, the endpoint, and the
exact body `BaseClient` will send. `wire_body` mirrors `transport._encode`, so
what you see is what goes out.

    t05  -> FEE: ERROR t05: FEE needs feeCode, none present

means `choose` selected a type whose model needs a field the record does not
have. That is a condition broader than the model it selects: either narrow the
condition or make the field optional.

    1  no match: t06

means `choose` returned `None`. Listed by id rather than forced into the
nearest type.

Under `--post`, failures are split. `REJECTED` is a 4xx: the body is wrong and
will be wrong the same way next time, usually the model or the mapping.
`FAILED` is a connection error, timeout, 5xx or rate limit.

## Not included

No checkpoint, no idempotency key, no retry of failed writes, no scheduling.
**Re-running posts everything again.** Point it at a test environment.
