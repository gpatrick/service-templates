"""What happened, in a form a scheduler and a person can both act on.

Two audiences, two outputs.

The scheduler gets an exit code. It is the only thing most schedulers can
alert on, so the codes are few and their meanings do not overlap:

    0  everything attempted succeeded or was deliberately skipped
    1  the run completed, but some records were set aside as dead letters
    2  the run stopped early: failure threshold crossed, or interrupted
    3  the run never really started: bad configuration or credentials
    4  the run happened but its state could not be written to disk

1 and 2 are separated because they need different responses. A 1 means read
the dead-letter file at your convenience. A 2 means something is wrong now and
the next scheduled run will probably hit it too.

4 exists because without it an unwritable checkpoint or dead-letter path exits
1 by accident: the OSError escapes, Python exits 1, and that is indistinguishable
from "completed with dead letters". The alert then says read the file at your
convenience, about a file that was never written. It is a distinct code because
it needs the opposite response from a 1.

The person gets the summary and the dead-letter file. Dead letters are JSONL,
one self-contained record per line, including the full source payload. That
matters because the point of a dead letter is to be fixed and resubmitted, and
a dead letter that only says "record t1 failed" requires going back to the
source to find out what t1 was. By then the window may have moved.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .records import SourceTransaction
from .selection import SkipReason

__all__ = ["DeadLetter", "RunReport", "ExitCode", "write_dead_letters"]


class ExitCode:
    OK = 0
    COMPLETED_WITH_DEAD_LETTERS = 1
    ABORTED = 2
    FATAL = 3
    OUTPUT_UNWRITABLE = 4


@dataclass(frozen=True)
class DeadLetter:
    """One record that will not be retried automatically."""

    account_id: str
    transaction_id: str
    stage: str  # "extract", "transform" or "load"
    failure: str  # "terminal" or "transient"
    error: str
    payload: dict[str, Any] | None = None
    occurred_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def to_json(self) -> str:
        return json.dumps(
            {
                "account_id": self.account_id,
                "transaction_id": self.transaction_id,
                "stage": self.stage,
                "failure": self.failure,
                "error": self.error,
                "payload": self.payload,
                "occurred_at": self.occurred_at,
            },
            default=_json_default,
            sort_keys=True,
        )

    @classmethod
    def from_source(
        cls,
        transaction: SourceTransaction,
        *,
        stage: str,
        failure: str,
        error: str,
    ) -> DeadLetter:
        return cls(
            account_id=transaction.account_id,
            transaction_id=transaction.transaction_id,
            stage=stage,
            failure=failure,
            error=error,
            payload={
                "business_date": transaction.business_date,
                "amount": transaction.amount,
                "currency": transaction.currency,
                "kind": transaction.kind,
                "description": transaction.description,
                # The complete upstream body, so the record can be replayed
                # without going back to the source. This is the field that
                # makes the file worth writing.
                "raw": dict(transaction.raw),
            },
        )


@dataclass
class RunReport:
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    finished_at: str | None = None
    accounts: int = 0
    records_read: int = 0
    posted: int = 0
    deduplicated: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    transient_failures: int = 0
    terminal_failures: int = 0
    extract_failures: int = 0
    dead_letters: list[DeadLetter] = field(default_factory=list)
    aborted_reason: str | None = None
    fatal_reason: str | None = None
    dry_run: bool = False

    # -- recording ---------------------------------------------------------

    def record_skip(self, reason: SkipReason) -> None:
        self.skipped[reason.value] += 1

    def record_dead_letter(self, letter: DeadLetter) -> None:
        self.dead_letters.append(letter)
        if letter.failure == "terminal":
            self.terminal_failures += 1
        else:
            self.transient_failures += 1
        if letter.stage == "extract":
            # Counted separately because it is not a write. An account that
            # could not be read produces no attempt against the destination,
            # and folding it into the write statistics makes the failure rate
            # describe something other than what the threshold is guarding.
            self.extract_failures += 1

    def finish(self) -> None:
        self.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # -- derived -----------------------------------------------------------

    @property
    def attempted(self) -> int:
        """Writes actually attempted. Skips and unread accounts are not.

        Two exclusions, for the same reason. On a re-run almost everything is
        skipped as already synced, and counting those as successful attempts
        would dilute the rate until a broken run looked healthy. An account
        that could not be READ produces no write at all, and counting it as a
        failed attempt does the opposite: one unreadable account on a re-run
        where nothing else needed posting reads as a 100% failure rate, and
        with a low threshold floor that aborts a run in which the destination
        was never even contacted.
        """
        return self.posted + self.write_failures

    @property
    def failures(self) -> int:
        """Every dead letter, whatever stage produced it."""
        return self.transient_failures + self.terminal_failures

    @property
    def write_failures(self) -> int:
        """Failures that were actually attempts against the destination."""
        return self.failures - self.extract_failures

    @property
    def failure_rate(self) -> float:
        """Failed writes over attempted writes. The threshold guards this."""
        return self.write_failures / self.attempted if self.attempted else 0.0

    @property
    def exit_code(self) -> int:
        if self.fatal_reason:
            return ExitCode.FATAL
        if self.aborted_reason:
            return ExitCode.ABORTED
        if self.dead_letters:
            return ExitCode.COMPLETED_WITH_DEAD_LETTERS
        return ExitCode.OK

    # -- rendering ---------------------------------------------------------

    def render(self) -> str:
        lines = [
            "",
            "=" * 62,
            "  DRY RUN - nothing was posted" if self.dry_run else "  sync run",
            "=" * 62,
            f"  started    {self.started_at}",
            f"  finished   {self.finished_at or '(incomplete)'}",
            f"  accounts   {self.accounts}",
            f"  read       {self.records_read}",
            f"  posted     {self.posted}"
            + (f" ({self.deduplicated} deduplicated)" if self.deduplicated else ""),
        ]

        if self.skipped:
            lines.append(f"  skipped    {sum(self.skipped.values())}")
            for reason, count in sorted(self.skipped.items()):
                lines.append(f"               {count:>6}  {reason}")

        if self.write_failures:
            lines.append(
                f"  failed     {self.write_failures} "
                f"({self.terminal_failures} terminal, "
                f"{self.transient_failures} transient) "
                f"= {self.failure_rate:.1%} of {self.attempted} attempted"
            )
        if self.extract_failures:
            lines.append(
                f"  unread     {self.extract_failures} account(s) could not be read"
            )

        if self.fatal_reason:
            lines.append(f"  FATAL      {self.fatal_reason}")
        if self.aborted_reason:
            lines.append(f"  ABORTED    {self.aborted_reason}")

        lines.append(f"  exit code  {self.exit_code}")
        lines.append("=" * 62)
        return "\n".join(lines)


def write_dead_letters(letters: list[DeadLetter], path: Path) -> int:
    """Append dead letters as JSONL. Returns the number written.

    Append rather than overwrite: a run that fails the same record every day
    should produce a growing record of that, not a file that always looks like
    it contains one problem. Rotation is the scheduler's job, not this
    program's.
    """
    if not letters:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for letter in letters:
            handle.write(letter.to_json())
            handle.write("\n")
    return len(letters)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        # str, not float. A dead letter is meant to be replayed, and a
        # round-trip through binary floating point can change the amount.
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
