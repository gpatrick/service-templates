# DECISIONS

Why the job is shaped this way. Written for whoever changes it next, including
whoever inherits it from the client's team.

## The batch job and the web API need opposite things

Both consume `service-client`, and almost every tuning decision differs.

| | Web API | txn-sync |
|---|---|---|
| Retry budget | 20s, under the gateway | 30s, nothing is waiting |
| Read attempts | 3 | 4 (`ACCOUNTS_RETRY`) |
| Failure granularity | Per request | Per record, with a rate threshold |
| Retry mechanism for writes | None; surface 504 | Re-run with the same key |
| Failure signal | HTTP status | Exit code |
| Central concern | Latency under a timeout | Not posting anything twice |

That divergence is the argument for keeping `RetryPolicy` a constructor
parameter rather than a module constant, and for `BaseClient` knowing nothing
about either application.

## Deterministic idempotency keys are the safety property

A scheduled job gets re-run, and that re-run is a retry of every record it
covers, including those whose response was lost after the destination had
already applied them. The checkpoint cannot cover that case: such a record is
absent from the checkpoint and present in their ledger.

The key is derived from source system, account id, transaction id and business
date, so the same logical record produces the same key forever. `uuid4()` is
the obvious reflex and is exactly wrong, because a fresh key per attempt makes
every retry a new logical operation.

`KEY_VERSION` exists so a change to the derivation is visible rather than
catastrophic. Changing what goes into the hash changes every key, which means
records already posted under the old scheme can post again, so a version bump
needs a plan for the overlap window rather than just a code change.

## Both the key and the checkpoint are scoped by account

Source systems frequently number transactions per account, so two accounts can
each hold a transaction called `t1`. Scoped on transaction id alone, the
checkpoint skips one and the idempotency key has the destination deduplicate
the other. Money silently not moved, with a success in the log, which is the
hardest failure to notice.

There are three regression tests named for this: two unit tests in
`test_keys.py` and `test_checkpoint.py`, and one end-to-end in
`test_runner_happy.py`. Do not delete them.

## The checkpoint is an optimization, not the correctness mechanism

What prevents duplicates is the idempotency key. The checkpoint exists so a
run that died three quarters of the way through does not spend the next run's
whole budget reposting work the destination would only deduplicate anyway.

Keeping that straight matters when someone proposes deleting the checkpoint to
force a full re-sync. That is safe precisely because the keys are
deterministic. If the keys were per-attempt, it would not be.

Corollaries:

- The file is written atomically (temp file in the same directory, fsync,
  `os.replace`). A truncated JSON file leads someone to delete it, which
  reposts a window.
- A corrupt or version-mismatched file raises rather than starting from empty.
  Starting empty may well be right, but it is a person's decision.
- Records are marked done only after the destination confirms. Marking on
  dispatch turns a lost response into a permanently skipped record.

## Reads concurrent, writes serial

Serial writes keep the failure-rate threshold meaningful. With concurrent
writes the threshold is only observed once the in-flight batch lands, so a run
can overshoot it by the width of the batch. For money-adjacent records,
overshooting is the wrong direction to be imprecise in.

The queue between them is bounded. Unbounded, a fast source and a slow
destination become memory growth that ends the run with an OOM kill and no
summary. Bounded, the extract side blocks, which is correct backpressure and
costs nothing.

If throughput ever binds, the fix is a destination-side bulk endpoint, not
parallel singles.

## Write retries are off by default

`SYNC_RETRY_WRITES_ON_TRANSIENT=false`. With it off, a transient write failure
is left for the next scheduled run, which re-derives the same key. With it on
and a destination that ignores the idempotency header, every transient blip
creates a duplicate and nothing fails visibly.

The switch is worth having, because a retry with a genuinely honored key is
strictly better than waiting a day. But the default has to be the safe one,
and turning it on has to require someone to have answered a specific question
first.

When it is on, every attempt uses the same key. That is the entire reason the
retry is defensible.

## Four levels of failure handling

1. **Inside one write.** Optional retry with the same key. Off by default.
2. **Per record.** Transient failures stay out of the checkpoint so the next
   run reconsiders them. Terminal failures are dead-lettered with the full
   source payload and the run continues.
3. **Per run.** A failure rate above the threshold aborts. There is a
   minimum-attempts floor, because without it the first failed record is a
   100% failure rate and every run with an early blip aborts.
4. **Immediately.** Fatal errors (bad credentials, unfetchable token) stop
   everything. Every subsequent call would fail identically, so burning the
   threshold to rediscover that wastes a run and fills the dead-letter file
   with noise.

An unrecognized exception is classified terminal rather than transient. An
unknown error retried forever is a silent infinite loop across runs; an
unknown error dead-lettered is a line in a file someone will read.

## Skips and unread accounts are not attempts

The failure rate is failed writes over attempted writes. Two things are
excluded, for opposite reasons.

A skip is not an attempt. On a re-run almost everything is skipped as already
synced, and counting those as successful attempts would dilute the rate until a
broken run looked healthy.

An account that could not be READ is not an attempt either, and this one was a
bug until an end-to-end run exposed it. Extract failures were folded into the
write statistics, so a re-run where everything else was already synced reported
a 100% failure rate over one attempt, having never contacted the destination.
With a low threshold floor that aborts a run in which no write was tried. Both
counts are still surfaced, on separate lines in the summary, because an
unreadable account is worth seeing; it just is not evidence about the
destination.

## Skips are named, not silent

Every skip has a reason from a fixed enum and lands in the summary. Without
that, the only way to find out where 388 of 400 records went is to add logging
and wait for tomorrow's run.

The reasons also do double duty as a diagnostic: a summary that is mostly
`outside_window` means the source is ignoring the date parameters, which is
the quiet failure that a wrong query parameter name produces.

## The window is half-open and anchored on today

Inclusive-end ranges make consecutive runs either overlap by a day or skip
one, depending on which mistake you make, and both are quiet. Half-open, one
run's `end` is the next run's `start` and neither problem is expressible.

Anchoring on today rather than on the previous run means a skipped run does
not silently shrink the next one's coverage. Widening `SYNC_WINDOW_DAYS` is
the cheap insurance, because overlap is free.

## The core imports no adapter and no HTTP client

Everything in `src/txn_sync/` except `adapters/` and `wiring.py` depends only
on the two protocols in `contracts.py`. Three payoffs:

- The whole pipeline is testable with in-memory fakes, so the retry,
  threshold, resume and dead-letter paths get real coverage here rather than
  being discovered against the client's systems on a bad day.
- The handover is confined to two adapter files plus `transform.py`.
- The two systems version independently; a rename on one side does not ripple
  into code dealing with the other.

`tests/test_layering.py` enforces it in a subprocess, because import side
effects are global and one convenience import would lose the property
quietly.

## Dry run is a destination implementation, not a flag in the runner

`wiring.build_destination` returns a `DryRunDestination` when `dry_run` is set, so there is
no code path in which a rehearsal holds a live destination client at all. A
guard inside the runner would be one `if` away from posting for real; not
constructing the client is a stronger guarantee.

The runner additionally refuses to write the checkpoint in dry-run mode, so a
rehearsal cannot make the real run skip records it never posted.

## Two auth objects, not one

`service-client`'s own docstring describes sharing one auth object between the
Accounts and Payments clients, which is right there: two services behind one
gateway. It is wrong here. The source and destination are separate systems
with separate credentials, and sharing would send the source's token to the
destination.

## Configuration reports every problem at once

Failing on the first missing variable turns configuring a scheduled job into a
guessing loop. On a client's machine, where each cycle may mean a ticket, that
is the difference between an afternoon and a week.

Secrets are never logged. `redacted()` is printed to stderr as soon as the
config validates and before anything else can fail, because "what was this run
pointed at" is the first question asked about a failed scheduled job.

## Exit codes are few and do not overlap

Most schedulers can only alert on an exit code. 1 (dead letters, read the file
when convenient) and 2 (stopped early, something is wrong now) are separated
because they need different responses, and 3 outranks both so a credentials
problem is never masked by the run also having dead letters.

4 was added after the systemd work, for an unwritable checkpoint or dead-letter
path. It exists because the alternative is not "no code" but "exit 1 by
accident": an OSError escaping main means Python exits 1, which reads as
completed-with-dead-letters and sends someone to read a file that does not
exist. The two situations need opposite responses, so they cannot share a code.

That bug was latent until deployment made it likely. `ProtectSystem=strict`
plus the relative default paths is exactly the combination that produces an
unwritable state path on a first install.

## Dead letters carry the full source payload

The point of a dead letter is to be fixed and replayed. One that says only
"record t1 failed" requires going back to the source, and by then the window
may have moved. Amounts serialize as strings rather than JSON numbers, because
a round trip through binary floating point can change the amount.

The file is appended, never overwritten, so a record that fails every day
leaves a growing trail.

## Money is Decimal, everywhere

Never `float`. Binary floating point cannot represent most decimal amounts
exactly and the error compounds across a summed batch. This holds at the model
boundary, through the transform, in the dead-letter file, and in the request
body, where amounts are serialized as JSON strings.

## The transform is pure and total

No I/O, no clock, no randomness, and every record either maps or raises. A
transform that consults the current time cannot be replayed, and replay is how
a dead-lettered record gets fixed. A transform that can return `None` is a
place for records to disappear without appearing in any skip count; dropping
records is a rule, and rules belong in `selection.py` where the skip is counted
and named.
