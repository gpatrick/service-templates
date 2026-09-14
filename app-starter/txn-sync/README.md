# txn-sync

A scheduled job that reads transactions from a source system and posts them
into a destination system. Extract, transform, load, with the failure handling
a money-adjacent batch job needs.

It is a separate project from the FastAPI service. Both consume
`service-client`, and nothing else is shared. They are worth reading side by
side: a request-driven API and a scheduled job need almost opposite things
from the same library. The API races a gateway timeout and keeps retry budgets
tight; the job has no such pressure but has to treat re-running as its retry
mechanism, which makes idempotency the central concern rather than an
optimization.

## Reading order

    src/txn_sync/
      records.py             the record shapes everything else is written against
      contracts.py           the seams between the core and the two systems
      keys.py                deterministic idempotency keys. READ THIS ONE FIRST
      selection.py           which records post, and why the rest are skipped
      transform.py           source shape to destination shape. Main fill-in point
      checkpoint.py          resume state
      runner.py              orchestration, four levels of failure handling
      report.py              outcomes, dead letters, exit codes
      config.py              environment config, validated before any network call
      adapters/
        source_http.py       the source system. CUSTOMIZE
        destination_http.py  the destination system. CUSTOMIZE
        memory.py            fakes, used by the tests and by --dry-run
      wiring.py              the only place that instantiates real adapters
      __main__.py            CLI, signals, exit codes
    deploy/systemd/          service unit, timer, failure routing

`HANDOFF.md` is the checklist for the client's machine.
`DECISIONS.md` explains why the job is shaped this way.

## The property to preserve

The core imports no adapter and no HTTP client. That is what makes the whole
pipeline testable without a network, and it is what makes the handover a
matter of editing two files rather than tracing behavior through the
orchestration. `tests/test_layering.py` enforces it in a subprocess, because a
single convenience import would otherwise lose it quietly.

## Shape of a run

accounts --> N extract tasks --> bounded queue --> 1 load task --> destination

Reads are concurrent because they are idempotent, independent per account, and
usually the slow part. Writes are serial, and that is a deliberate cost: with
concurrent writes a failure threshold is only observed once the in-flight
batch lands, so a run can overshoot it by the width of the batch. For records
going into another system's ledger, overshooting is the wrong direction to be
imprecise in.

If throughput becomes the binding constraint, the honest fix is a
destination-side bulk endpoint rather than parallel singles. Worth raising
with their team early.

## Requirements

Python 3.10 or newer. The code uses `X | None` throughout, which is a syntax
error on 3.9.

## Layout

`txn-sync` depends on `service-client`, which depends on `client-core`.
Neither is on an index, so both install from local paths. The Makefile expects
them as siblings:

    somewhere/
      client-core/
      service-client/
      txn-sync/

If they live elsewhere, adjust the `pip install -e "../..."` lines in the
Makefile, or install both yourself first. Order matters: client-core before
service-client.

`txn-sync` imports `BaseClient` and `RetryPolicy` through `service_client`,
which re-exports them. That still works and needs no change. New code should
import them from `client_core` directly.

## Install

    python3.12 -m venv .venv
    source .venv/bin/activate
    cd txn-sync
    make install

## Verify before changing anything

    make check

Expect 147 tests to pass. If they do not, fix that before touching the
adapters, because from here on a failure could be yours or theirs and you want
to know which.

    make verify

Installs into a throwaway venv and runs the tests there. `make check` runs
against whatever your working environment happens to contain, which hides
missing dependencies: a package installed for some other project satisfies an
import that `pyproject.toml` never declares. Run `verify` before handing the
project over and after touching dependencies.

## Configure

    cp .env.example .env

Fill it in. `.env` is gitignored; keep it that way. The file is commented with
what each value does and which ones are dangerous to change.

The program reads the environment, not `.env` directly. Export it however your
scheduler does, or use `set -a; . ./.env; set +a` in a shell.

## Running it

    python -m txn_sync --dry-run
    python -m txn_sync --accounts acct-1,acct-2
    python -m txn_sync --window-days 7 --as-of 2026-03-08
    python -m txn_sync --checkpoint /tmp/backfill.json --reset-checkpoint

`--as-of` and a scratch `--checkpoint` path are the backfill combination: they
let you re-run a past window without disturbing the scheduled job's resume
state.

Start with `make dry-run`. It reads and transforms everything, posts nothing,
never constructs the destination client, and writes neither the checkpoint nor
the dead-letter file. Anything it would have dead-lettered goes to stderr, so a
rehearsal leaves no line in a file that gets read during incidents. Read the skip reasons in the
summary before running anything that writes. The ordered list at the end of
`HANDOFF.md` covers the rest of the first real run.

## Exit codes

The job's interface to the scheduler.

    0  everything attempted succeeded or was deliberately skipped
    1  completed, but some records were set aside as dead letters
    2  stopped early: failure threshold crossed, or interrupted
    3  never really started: bad configuration or credentials
    4  the run happened but its state could not be written to disk

1 and 2 are separate because they need different responses. A 1 means read the
dead-letter file at your convenience. A 2 means something is wrong now and the
next scheduled run will probably hit it too.

4 covers an unwritable checkpoint or dead-letter path. It is a distinct code
because without it an OSError escapes and Python exits 1, which is
indistinguishable from "completed with dead letters" and sends someone to read
a file that was never written. When the dead-letter write is what failed, the
letters are dumped to stderr first so the journal still holds them.

Alert on 2, 3 and 4.

## Reading a run summary

    read       412
    posted     11
    skipped    401
                  398  already_synced
                    3  outside_window
    exit code  0

That is a healthy re-run: the window overlaps yesterday's, almost everything
is already across, and eleven new records moved.

Two summaries worth recognizing:

**Mostly `outside_window`.** The source is ignoring the date parameters, which
usually means the query parameter names in `adapters/source_http.py` are
wrong. An unrecognized query parameter is ignored rather than rejected, so the
call succeeds and returns the account's entire history. The job is behaving
correctly by skipping them, but the read is enormously more expensive than it
should be.

**A high `deduplicated` count.** The destination is telling you it has seen
these before. Usually the checkpoint was lost; occasionally the schedule is
firing twice.

**An `unread` line.** One or more accounts could not be read at all. It is
reported separately from failed writes because it is not evidence about the
destination, and it is excluded from the failure rate for the same reason.

## Dead letters

One self-contained JSON object per line, including the full source payload, so
a record can be fixed and replayed without going back to the source. By the
time someone reads the file, the window may have moved.

The file is appended, never overwritten. A record that fails every day should
leave a growing trail rather than a file that always looks like it holds one
problem. Rotation is the scheduler's job.

## Scheduling

The program has no scheduling loop. It runs one window and exits with a code,
so something external owns the cadence. Ready-to-install systemd units are in
`deploy/systemd/`, and `deploy/README.md` covers installation, verification and
day-to-day operation.

    sudo systemctl enable --now txn-sync.timer

Self-scheduling was considered and rejected. A daemon has to stay running,
which means you need supervision and restart-on-boot anyway, and the failure
mode inverts: when a self-scheduling job dies the schedule stops silently, and
a run that never happened is much harder to alert on than a run that failed.
It would also discard the exit codes, which only mean something to a caller
that waits for the process.

Whatever triggers it, five things have to hold:

- Runs cannot overlap. The checkpoint is not a lock, and two concurrent runs
  against one path interleave writes and lose entries. The systemd units get
  this for free, since a unit that is already active will not start again.
- The SIGTERM-to-SIGKILL window must exceed the destination write timeout,
  currently 30s with a 30s retry budget. The unit sets 90s. Too short and the
  process dies mid-write.
- The checkpoint path must survive between runs. If it is wiped, every run
  reposts its window: safe, because the keys are deterministic, but it burns
  the whole budget and fills the destination's logs with deduplicated writes.
- Alerting distinguishes exit 2 from 3 from 4 where the platform allows it. The
  `OnFailure` handler in `deploy/systemd/` does this by reading the real exit
  code, which systemd does not otherwise pass along.
- There is a ceiling on runtime, so a hung run cannot still be holding the
  unit when the next trigger fires.

Catch-up after downtime is deliberately off. The window is anchored on today
rather than on the last successful run, so widening `SYNC_WINDOW_DAYS` covers
an outage with fewer edge cases than replaying missed fires, and the overlap
costs nothing.

## Safety properties, in one place

1. **Idempotency keys are derived from the record**, not generated per
   attempt, so a re-run of an already-posted record is free. `keys.py`.
2. **Keys and checkpoint entries are scoped by account**, because source
   systems frequently number transactions per account. Two regression tests
   are named for this; do not delete them.
3. **The checkpoint is an optimization, not the correctness mechanism.**
   Deleting it is safe precisely because the keys are deterministic.
4. **Records are checkpointed only after the destination confirms.** Marking
   on dispatch would turn a lost response into a permanently skipped record.
5. **Writes are never retried automatically.** Enabling retries is a
   configured decision, and only sound once someone has confirmed the
   destination honors the idempotency header.

## Things to check before a release

- `make verify` passes in a clean environment.
- `IDEMPOTENCY_HEADER` in `adapters/destination_http.py` matches what the destination
  actually expects. If it ignores the header, every retry creates a duplicate
  and nothing visibly fails.
- The regression tests named for the account-scoping defect still exist:
  `test_keys_are_scoped_by_account_not_by_transaction_id_alone`,
  `test_checkpoint_is_scoped_by_account`, and
  `test_two_accounts_holding_the_same_transaction_id_both_post`.
- `test_a_write_is_never_retried_by_the_transport` still passes. If it does
  not, someone enabled POST retries and a lost response can become two
  transactions.
- `SYNC_RETRY_WRITES_ON_TRANSIENT` is false unless the idempotency header has
  been confirmed against a real environment.
- `tests/test_layering.py` passes. If it does not, the core has picked up a
  dependency on an adapter and is no longer testable without a network.
