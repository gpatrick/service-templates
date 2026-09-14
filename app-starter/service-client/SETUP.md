# Pushing this to GitHub

```bash
cd service-client
git init
git add .
git commit -m "Accounts and Payments clients: transport, auth, tests"
git branch -M main
git remote add origin git@github.com:YOU/service-client.git
git push -u origin main
git tag v0.1.0 && git push --tags
```

Check `.gitignore` before the first commit. `samples/`, `schemas/`, and `.env`
are already excluded; recorded API responses are real payloads and should not
land in the repo unscrubbed.

## On the other side

```bash
git clone git@github.com:YOU/service-client.git
cd service-client
python3.12 -m venv .venv && source .venv/bin/activate
make install
make test          # 101 tests should pass before you change anything
```

**Python 3.10 or newer is required.** The code uses `X | None` throughout,
which is a syntax error on 3.9. `make install` checks the version and says so
rather than letting pip fail confusingly further down.

A venv is bound to the interpreter that created it, so switching versions means
recreating it, not upgrading it:

```bash
deactivate && rm -rf .venv
python3.12 -m venv .venv && source .venv/bin/activate
make install
```

On macOS, Homebrew does not symlink versioned Pythons to `python3`, so call
`python3.12` explicitly when creating the venv. `uv venv --python 3.12` does
the same job and will fetch the interpreter if you do not have it.

If `pip install -e` complains that setup.py was not found, pip is older than
21.3. `make install` upgrades pip first, so this should not recur inside a
fresh venv.

Then, in order:

1. Rename the package (see README).
2. Confirm the assumptions listed at the top of `accounts.py` and
   `payments.py`.
3. Copy `.env.example` to `.env` and fill it in.
4. `make samples`, then `make models`. Review the diff.
5. Adjust the endpoint methods, then rewrite `test_accounts.py` and
   `test_payments.py`.
6. Define your own contract in `examples/api/models.py` and the mappers that
   fill it. Endpoints must never return a client model directly.

## Environment

The FastAPI example reads four variables at startup, so a missing one fails
before the server accepts traffic:

```
SERVICE_BASE_URL
SERVICE_TOKEN_URL
SERVICE_CLIENT_ID
SERVICE_CLIENT_SECRET
```

`make samples` additionally uses `SERVICE_TOKEN`, a bearer token for recording
responses by hand.

## Before the demo

- `make check` (lint, types, tests) passes.
- `make verify` passes. This installs into a throwaway venv from
  `pyproject.toml` alone, which catches dependencies that your working
  environment happens to satisfy but the project never declares. Run it after
  touching dependencies and before tagging.
- The retry budget in `examples/fastapi_app.py` is under the gateway's
  timeout. Currently 6s per request, 20s total for Accounts, against an
  assumed 30s gateway limit.
- `test_transfer_is_never_retried` still passes. If it does not, someone
  enabled POST retries and transfers can double-fire.

## Per-request headers on POST (added for txn-sync)

`BaseClient._post` takes an optional `headers` mapping, forwarded to the
request. It exists so a caller can send an idempotency key, which is
per-request by nature and cannot live in the client's constructor headers.

It is a named parameter rather than part of `**params` deliberately. `**params`
becomes the query string, so a caller passing `headers={...}` without this
would have the value silently serialized as a query parameter and the header
never sent. Nothing would error, because an API ignores an unrecognized query
parameter and returns 200. `test_per_request_headers_are_not_sent_as_query_parameters`
guards exactly that.

The change is additive. No existing call site or behavior changed.
