"""Command line entry point.

    python -m txn_sync --dry-run
    python -m txn_sync --accounts acct-1,acct-2 --window-days 3
    txn-sync                                  (installed console script)

Exit codes are the job's interface to the scheduler; see report.py. Nothing
here should print to stdout except the summary and the dry-run preview, so
that a scheduler capturing stdout gets something worth reading in an alert.
Diagnostics go to stderr.

SIGTERM and SIGINT request a graceful stop rather than killing the process.
Schedulers send SIGTERM before SIGKILL on a timeout, and the window between
them is exactly enough to save the checkpoint. A job that ignores SIGTERM
throws that away and reposts on the next run.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import signal
import sys
from datetime import date, datetime
from pathlib import Path

from .checkpoint import Checkpoint
from .config import SyncConfig, load_config
from .errors import FatalFailure
from .report import ExitCode, RunReport, write_dead_letters
from .runner import SyncRunner
from .wiring import build_destination, build_source, close_quietly

__all__ = ["main"]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="txn-sync",
        description="Copy transactions from the source system to the destination.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read and transform everything, post nothing, write no checkpoint",
    )
    parser.add_argument(
        "--accounts",
        help="comma separated account ids, overriding SYNC_ACCOUNT_IDS",
    )
    parser.add_argument(
        "--window-days", type=int, help="override SYNC_WINDOW_DAYS for this run"
    )
    parser.add_argument(
        "--as-of",
        help=(
            "treat this ISO date as today when computing the window. "
            "For backfills and for reproducing a past run."
        ),
    )
    parser.add_argument(
        "--checkpoint",
        help="override the checkpoint path (use a scratch path for backfills)",
    )
    parser.add_argument(
        "--reset-checkpoint",
        action="store_true",
        help=(
            "forget resume state before running. Safe: records already posted "
            "are deduplicated by their idempotency key, which is derived from "
            "the record rather than generated per attempt."
        ),
    )
    return parser.parse_args(argv)


def _apply_overrides(config: SyncConfig, args: argparse.Namespace) -> SyncConfig:
    """Apply CLI flags on top of the environment configuration.

    One dataclasses.replace per flag rather than a collected dict. A dict of
    mixed value types defeats the type checker on the way in, which matters
    here because a typo in a field name would otherwise be caught only at
    runtime, on the client's machine, in a scheduled job.
    """
    if args.dry_run:
        config = dataclasses.replace(config, dry_run=True)

    if args.accounts:
        accounts = tuple(
            sorted(part.strip() for part in args.accounts.split(",") if part.strip())
        )
        if not accounts:
            raise FatalFailure("--accounts was given but contained no account ids")
        config = dataclasses.replace(config, account_ids=accounts)

    if args.window_days is not None:
        if args.window_days < 1:
            raise FatalFailure("--window-days must be at least 1")
        config = dataclasses.replace(config, window_days=args.window_days)

    if args.checkpoint:
        config = dataclasses.replace(config, checkpoint_path=Path(args.checkpoint))

    return config


def _as_of(raw: str | None) -> date:
    if not raw:
        return date.today()
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise FatalFailure(f"--as-of must be an ISO date (YYYY-MM-DD): {exc}") from exc


async def _run(config: SyncConfig, today: date, reset: bool) -> RunReport:
    from .adapters.memory import DryRunDestination

    checkpoint = Checkpoint.load(config.checkpoint_path)
    if reset:
        checkpoint.clear()

    source = build_source(config)
    destination = build_destination(config)

    runner = SyncRunner(
        source=source, destination=destination, config=config, checkpoint=checkpoint
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, runner.request_stop)
        except NotImplementedError:  # pragma: no cover - Windows
            # add_signal_handler is POSIX only. On Windows the job still runs;
            # it just cannot shut down gracefully, which means a re-run redoes
            # the tail of the window. Safe, only wasteful.
            pass

    try:
        report = await runner.run(today)
    finally:
        await close_quietly(source, destination)

    if isinstance(destination, DryRunDestination):
        preview = destination.summary()
        if preview:
            print("\n  would post:")
            for line in preview:
                print(f"    {line}")

    return report


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        config = _apply_overrides(load_config(), args)
    except FatalFailure as exc:
        print(f"txn-sync: {exc}", file=sys.stderr)
        return ExitCode.FATAL

    # Emitted as soon as the configuration is known to be valid, and before
    # anything else can fail. "What was this run actually pointed at" is the
    # first question asked about a failed scheduled job, and it should be
    # answerable even when the failure came from a later flag.
    print(f"txn-sync: {config.redacted()}", file=sys.stderr)

    try:
        today = _as_of(args.as_of)
    except FatalFailure as exc:
        print(f"txn-sync: {exc}", file=sys.stderr)
        return ExitCode.FATAL

    try:
        report = asyncio.run(_run(config, today, args.reset_checkpoint))
    except FatalFailure as exc:
        print(f"txn-sync: fatal: {exc}", file=sys.stderr)
        return ExitCode.FATAL
    except ValueError as exc:
        # Raised by Checkpoint.load for a corrupt or version-mismatched file.
        # Fatal rather than recoverable on purpose: starting from an empty
        # checkpoint would repost the window, and that is a person's decision.
        print(f"txn-sync: fatal: {exc}", file=sys.stderr)
        return ExitCode.FATAL
    except KeyboardInterrupt:  # pragma: no cover - a second Ctrl-C
        print("txn-sync: interrupted", file=sys.stderr)
        return ExitCode.ABORTED
    except OSError as exc:
        # The checkpoint save in the runner's finally block could not write.
        # Distinct from every other code: the work happened, and the record of
        # it did not. Without this the OSError escapes and Python exits 1,
        # which reads as "completed with dead letters" and gets the opposite
        # response from the one it needs.
        print(f"txn-sync: could not write state: {exc}", file=sys.stderr)
        return ExitCode.OUTPUT_UNWRITABLE

    print(report.render())

    if config.dry_run:
        # A rehearsal must not mutate state, and the dead-letter file is
        # state: it is append-only and read during incidents, so a line from
        # a dry run is indistinguishable from a real failure. They go to
        # stderr instead, where they are visible without being recorded.
        for letter in report.dead_letters:
            print(letter.to_json(), file=sys.stderr)
        return report.exit_code

    try:
        written = write_dead_letters(report.dead_letters, config.dead_letter_path)
    except OSError as exc:
        # Dead letters exist only in memory until this call. Losing them
        # silently would leave failed records with no trace anywhere, so dump
        # them to stderr, where the journal captures them, before giving up.
        print(
            f"txn-sync: could not write {config.dead_letter_path}: {exc}. "
            f"Emitting {len(report.dead_letters)} dead letter(s) below instead.",
            file=sys.stderr,
        )
        for letter in report.dead_letters:
            print(letter.to_json(), file=sys.stderr)
        return ExitCode.OUTPUT_UNWRITABLE

    if written:
        print(
            f"  {written} dead letter(s) appended to {config.dead_letter_path}",
            file=sys.stderr,
        )

    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
