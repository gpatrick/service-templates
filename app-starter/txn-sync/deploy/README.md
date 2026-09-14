# Deploying txn-sync under systemd

The program has no scheduling loop. It runs one window and exits with a code,
which is what makes systemd the right owner of the cadence: a missed run shows
up as a timer that did not fire, and a failed run shows up as an exit status.
A self-scheduling daemon would hide both, because when it dies the schedule
silently stops and the absence of a run is much harder to alert on than the
failure of one.

The paths below assume `/opt/txn-sync`. Change them consistently in all three
unit files if you deploy elsewhere.

## Install

    # user, owning nothing but its own state
    sudo useradd --system --no-create-home --shell /usr/sbin/nologin txn-sync

    # code
    sudo mkdir -p /opt/txn-sync
    sudo rsync -a ./ /opt/txn-sync/
    sudo python3.12 -m venv /opt/txn-sync/.venv
    sudo /opt/txn-sync/.venv/bin/pip install -e /opt/service-client
    sudo /opt/txn-sync/.venv/bin/pip install -e /opt/txn-sync
    sudo chown -R root:root /opt/txn-sync

    # configuration, readable only by root (systemd reads it before dropping
    # privileges, so the service user never needs access to the secrets)
    sudo mkdir -p /etc/txn-sync
    sudo cp .env.example /etc/txn-sync/env
    sudo chmod 0600 /etc/txn-sync/env
    sudo chown root:root /etc/txn-sync/env
    sudo "${EDITOR:-vi}" /etc/txn-sync/env

    # units
    sudo cp deploy/systemd/txn-sync.service \
            deploy/systemd/txn-sync.timer \
            deploy/systemd/txn-sync-failure@.service \
            /etc/systemd/system/
    sudo systemctl daemon-reload

## Point state at the StateDirectory

`StateDirectory=txn-sync` gives the unit `/var/lib/txn-sync`, created with the
right ownership and surviving reboots. Two lines in `/etc/txn-sync/env` have to
agree with it, and the defaults in `.env.example` are relative paths that will
not:

    SYNC_CHECKPOINT_PATH=/var/lib/txn-sync/checkpoint.json
    SYNC_DEAD_LETTER_PATH=/var/lib/txn-sync/dead-letters.jsonl

Getting this wrong is not loud. With `ProtectSystem=strict` a relative path
resolves under a read-only `/opt/txn-sync`, the write fails, and the job exits
4, which is the good case and the reason that code exists. The bad case is a path that happens to be
writable but ephemeral: every run then reposts its whole window, succeeds, and
fills the destination's logs with deduplicated writes while looking healthy.

## Verify before enabling the timer

Run it once by hand, with the real configuration, in dry-run mode:

    sudo systemd-run --uid=txn-sync --pty \
      --property=EnvironmentFile=/etc/txn-sync/env \
      --property=StateDirectory=txn-sync \
      /opt/txn-sync/.venv/bin/txn-sync --dry-run

Read the skip reasons before going further. A summary dominated by
`outside_window` means the source is ignoring the date parameters and returning
full history; see `HANDOFF.md`.

Then a real single run, still without the timer:

    sudo systemctl start txn-sync.service
    systemctl status txn-sync.service
    journalctl -u txn-sync.service -n 50

The summary goes to stdout and the redacted configuration to stderr, so both
land in the journal. The configuration line is what answers "what was this run
pointed at" during an incident, and it carries no secrets.

## Enable

    sudo systemctl enable --now txn-sync.timer
    systemctl list-timers txn-sync.timer

## What the units already handle

**Overlap.** systemd will not start a unit that is already active, so a long
run cannot be trampled by the next trigger. This matters: the checkpoint is not
a lock, and two concurrent runs against one path interleave writes and lose
entries. No `flock` wrapper is needed.

**The shutdown window.** `TimeoutStopSec=90` gives the runner time to drain and
save its checkpoint after SIGTERM. It exceeds the destination client's 30s
timeout plus its 30s retry budget. Trimming it converts a clean stop into a
kill mid-write.

**Severity routing.** `OnFailure` hands off to `txn-sync-failure`, which reads
the real exit code and separates "dead letters, read them tomorrow" from
"stopped early, look now" from "the state path is unwritable". Exit 1 is
deliberately not in `SuccessExitStatus`; treating dead letters as success would
make them invisible here.

Exit 4 is the one to read carefully. It means the run did its work and could
not record it. If the dead-letter write was what failed, the letters were
dumped to the journal instead, so recover them from there before the next run
overwrites the context.

**Catch-up, by not doing it.** `Persistent=false`. The window is anchored on
today rather than on the last successful run, so widen `SYNC_WINDOW_DAYS` to
cover however long an outage might plausibly last. Overlap is free.

## Operating it

    # what happened last night
    journalctl -u txn-sync.service --since yesterday

    # dead letters, one self-contained JSON object per line
    sudo -u txn-sync tail -5 /var/lib/txn-sync/dead-letters.jsonl | jq .

    # a backfill, without disturbing the scheduled job's resume state
    #
    # systemd-run rather than a bare sudo: a manual invocation escapes the
    # unit, and with it the overlap protection. A backfill running alongside a
    # timer-triggered run means two processes writing the same dead-letter file
    # and two hitting the destination at once. The scratch --checkpoint keeps
    # the resume state separate; the transient unit keeps everything else from
    # colliding.
    sudo systemd-run --uid=txn-sync --pty --collect \
      --property=EnvironmentFile=/etc/txn-sync/env \
      --property=StateDirectory=txn-sync \
      --property=UMask=0077 \
      --setenv=SYNC_DEAD_LETTER_PATH=/var/lib/txn-sync/backfill-dead-letters.jsonl \
      /opt/txn-sync/.venv/bin/txn-sync \
        --as-of 2026-03-08 --window-days 7 \
        --checkpoint /var/lib/txn-sync/backfill-checkpoint.json

    # if the timer is running, stop it first rather than racing it
    sudo systemctl stop txn-sync.timer

    # pause without uninstalling
    sudo systemctl disable --now txn-sync.timer

Rotate the dead-letter file. It is appended, never truncated, so a record that
fails every day leaves a growing trail on purpose. A `logrotate` stanza with
`copytruncate` is fine; the program holds no handle on it between runs.

## If they are not on a plain VM

The same contract works anywhere that can run a command and read an exit code.
On Kubernetes, a `CronJob` with `concurrencyPolicy: Forbid` and
`terminationGracePeriodSeconds: 90` covers the same two requirements, with a
PVC for the state directory. One caveat: Kubernetes only distinguishes zero
from non-zero, so the 1/2/3 separation collapses at the platform level and has
to be surfaced through a log-based alert instead.

If they already run Airflow, Dagster, or Prefect, use it and get run history
and retries for free. Invoke the job as a subprocess or a container, never as a
Python function inside a worker: importing it loses the exit-code contract and
the signal handling, and couples this job's dependencies to the orchestrator's.
