# HANDOFF

What has to be filled in or confirmed on the client's machine, in the order
worth doing it. Everything here is a question about their two systems that
could not be answered from outside.

Three files change: `adapters/source_http.py`, `adapters/destination_http.py`, and
`transform.py`. Nothing in the core should need to move. If you find yourself
editing `runner.py` or `keys.py` to accommodate a real API, stop and check
whether the adapter is the right place instead.

---

## 0. A change was made to service-client

The zip this was built against predates per-request header support.
`BaseClient._post` accepted `**params`, which becomes the query string, so
passing `headers={"Idempotency-Key": ...}` sent the key as a QUERY PARAMETER
and never as a header. Nothing would have failed: the destination ignores an
unrecognized query parameter, returns 200, and every retry creates a
duplicate.

`transport.py` now takes an optional `headers` on `_request` and `_post`. The
change is additive and three tests cover it, one asserting specifically that
the key does not land in the query string. The library's suite went from 101
to 104 passing and no existing behavior changed, so the FastAPI service is
unaffected.

If the client already has a newer `service-client` than the zip, reconcile
before installing: check whether `_post` accepts `headers` and, if it does,
keep theirs.

---

## 1. The destination's transactions endpoint

`src/txn_sync/adapters/destination_http.py`

- [ ] `TRANSACTIONS_PATH`. Currently `/transactions`, which is a guess.
- [ ] `TransactionRequest` fields and their JSON aliases. Currently camelCase
      on the assumption of a typical JSON API.
- [ ] `TransactionResponse`. In particular whether the destination reports a
      deduplicated write, and under what field.
- [ ] Regenerate both models from recorded responses once samples exist. Same
      rules as `service-client`'s models: generated from a sample corpus, money
      as `Decimal` never `float`, never hand edited.

## 2. The idempotency header

`src/txn_sync/adapters/destination_http.py`, `IDEMPOTENCY_HEADER`

This is the single most consequential constant in the project.

- [ ] Confirm the header name. `Idempotency-Key` is the common convention
      (Stripe, and the IETF draft), but some APIs use `X-Idempotency-Key`,
      some expect a body field, and some have no such concept.

**How to confirm it:** against a non-production environment, post the same
body twice with the same key and check whether a second record is created. Do
not infer it from documentation alone.

- [ ] Record the answer somewhere the client's team will find it, because it
      determines whether `SYNC_RETRY_WRITES_ON_TRANSIENT` can ever be turned
      on.

If the destination has no idempotency concept at all, say so explicitly rather
than leaving the header in place doing nothing. The job still works, but the
duplicate-on-lost-response case becomes real, and the right response is
probably a read-back-before-write in the destination adapter. That is a design
conversation, not a config change.

## 3. The source's transactions endpoint

`src/txn_sync/adapters/source_http.py`

- [ ] Query parameter names for the date window. `AccountsClient` sends
      `startDate` and `endDate`. A wrong name is IGNORED, not rejected, so the
      call succeeds and returns the whole history. The symptom is a run
      summary that is mostly `outside_window` skips.
- [ ] The date format. ISO `YYYY-MM-DD` is assumed.
- [ ] Whether the API's `endDate` is inclusive. `DateWindow.end` is exclusive;
      if theirs is inclusive, one extra day comes back and shows up as
      `outside_window` skips rather than as bad data.
- [ ] Pagination. `iter_transactions` assumes a cursor in `nextCursor` and a
      list under `transactions`. If the endpoint returns a bare array, change
      `TransactionList` to a `RootModel` and adjust the paging loop.

## 4. The business date

`src/txn_sync/adapters/source_http.py`, `_business_date`

Currently the UTC date of `posted_at`. This is the assumption in the whole
project most likely to be wrong, because business date is a ledger concept
rather than a clock reading: cut-off times, local timezones and weekend rules
all move it.

- [ ] Does the source expose an explicit business or value date? If so, use
      that field and delete the fallback.
- [ ] If not, confirm the timezone and cut-off with their team.

It matters beyond the field itself: the business date is part of every
idempotency key, so changing this later changes every key, and records already
posted can post again.

## 5. Amounts

- [ ] `AMOUNTS_ARE_MINOR_UNITS` in `source_http.py`. Does the source send 1234
      meaning 12.34? A factor-of-100 error posts successfully and reconciles
      wrongly.
- [ ] `_signed_amount` in `transform.py`. **The most common way an integration
      like this goes wrong quietly.** Three conventions are in common use:
      signed with debits negative, signed with debits positive, and unsigned
      magnitude plus a direction field. Crossing between any two without
      noticing produces records that post cleanly and reconcile wrongly, with
      no error anywhere and a discrepancy that surfaces weeks later in someone
      else's report.

## 6. Field mapping

`src/txn_sync/transform.py`

- [ ] Fill in `FIELD_NOTES` with the real correspondence, one line per
      destination field, including constants and fields with no source
      analogue. That table is the artifact their team will actually review.
- [ ] `DESCRIPTION_MAX_LEN`. Currently 140, a guess. Destinations usually cap
      free text and reject overruns with a 400 that names the field but not
      the limit.
- [ ] `_extra_fields`. Ledger codes, posting channels, batch references.
      Empty by default so the first integration test posts the minimum viable
      body and the destination tells you what else it wants, which is faster
      than reading a spec that may be out of date.

## 7. Selection rules

`SYNC_EXCLUDED_KINDS`, `SYNC_ALLOWED_CURRENCIES`

- [ ] Which source record types must not cross over at all. Pending
      authorizations and memo-only lines are the usual candidates. Get this
      from the client rather than guessing; the cost of guessing wrong in
      either direction is a reconciliation problem.
- [ ] Which currencies the destination accepts.
- [ ] Whether reversals need special handling. The source model carries a
      `reversalOf` field. Right now a reversal is posted like any other
      record, which is correct if the destination treats it as an ordinary
      signed entry and wrong if it expects a link to the original.

## 8. Operational

- [ ] Checkpoint path on a persistent volume. If it lives somewhere that is
      wiped between runs, every run reposts the window. That is safe, because
      of the deterministic keys, but it wastes the whole budget and fills the
      destination's logs with deduplicated writes.
- [ ] Confirm the schedule cannot overlap itself. The checkpoint is not a lock;
      two concurrent runs against the same path interleave writes and lose
      entries. If overlap is possible, take a lock outside this program or give
      each run its own path.
- [ ] Alert on exit codes 2 and 3.
- [ ] Rotate the dead-letter file.
- [ ] `SYNC_WINDOW_DAYS` wide enough to cover a missed run. Overlap is free.

---

## First real run, in order

1. `make check`. 147 tests should pass before you change anything.
2. Fill in sections 1 and 3. Do not touch the header question yet.
3. `make dry-run` against the real source and a real destination base URL.
   Nothing is posted; the destination client is never even constructed.
4. **Read the skip reasons.** Mostly `outside_window` means the date
   parameters are wrong. Unexpected `zero_amount` or `unsupported_currency`
   counts mean the source shape is not what section 1 assumed.
5. Confirm the idempotency header (section 2) against a non-production
   destination.
6. Run against ONE account with a one-day window before all of them.
7. Check the destination for what actually landed, including the sign of the
   amounts, before widening.

Leave `SYNC_RETRY_WRITES_ON_TRANSIENT` at false through all of this.
