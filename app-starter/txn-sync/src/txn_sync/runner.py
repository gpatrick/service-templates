"""Orchestration. Concurrent reads, serial writes, four levels of failure.

SHAPE

    accounts --> N extract tasks --> bounded queue --> 1 load task --> destination

READS ARE CONCURRENT because they are idempotent, independent per account, and
usually the slow part. WRITES ARE SERIAL, and that is a deliberate cost.

The reason is the failure threshold. With concurrent writes, a threshold is
only observed once the in-flight batch lands, so a run can overshoot it by the
width of the batch before anything notices. For records being posted into
another system's ledger, overshooting is the wrong direction to be imprecise
in. Serial writes make "stop after the rate crosses 10%" mean exactly that.

If throughput ever becomes the binding constraint, the honest fix is a
destination-side bulk endpoint, not parallel singles. Say so during the
handover; it is a question worth asking their team early.

THE QUEUE IS BOUNDED. An unbounded queue turns a fast source and a slow
destination into memory growth that ends the run with an OOM kill and no
summary. Bounded, the extract side simply blocks, which is the correct
backpressure and costs nothing.

FOUR LEVELS OF FAILURE HANDLING

  1. Inside one write     Optional retry with the SAME idempotency key, only
                          when explicitly enabled. Off by default.
  2. Per record           Transient records are left out of the checkpoint for
                          the next run. Terminal ones are dead-lettered.
  3. Per run              A failure rate above the threshold aborts the run.
  4. Immediately          Fatal errors stop everything on the spot.

INTERRUPTION

A SIGTERM or SIGINT sets a stop event. Extract tasks stop producing, the load
task drains what is queued without posting it, and the checkpoint is saved. A
half-finished run that saved its checkpoint resumes cleanly; one that did not
reposts, which is safe but wasteful.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import date

from .checkpoint import Checkpoint
from .config import SyncConfig
from .contracts import TransactionDestination, TransactionSource
from .errors import (
    FatalFailure,
    RunAborted,
    TerminalFailure,
    TransientFailure,
    classify_failure,
)
from .keys import idempotency_key
from .records import DateWindow, SourceTransaction
from .report import DeadLetter, RunReport
from .selection import decide
from .transform import TransformError, transform

__all__ = ["SyncRunner"]


class SyncRunner:
    """One run of the job. Not reusable; construct a new one per run.

    Everything it touches from the outside world arrives through the
    constructor: the two ports, the checkpoint, and the clock. There is no
    module-level state and no import of either adapter, which is what lets the
    whole thing be exercised with in-memory fakes.
    """

    def __init__(
        self,
        *,
        source: TransactionSource,
        destination: TransactionDestination,
        config: SyncConfig,
        checkpoint: Checkpoint,
        report: RunReport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._source = source
        self._destination = destination
        self._config = config
        self._checkpoint = checkpoint
        self._report = report or RunReport(dry_run=config.dry_run)
        # Injectable so the retry tests do not spend real seconds sleeping.
        self._sleep = sleep or asyncio.sleep
        self._stop = asyncio.Event()
        self._since_save = 0

    # -- public ------------------------------------------------------------

    def request_stop(self) -> None:
        """Ask for a graceful shutdown. Safe to call from a signal handler."""
        self._stop.set()

    @property
    def report(self) -> RunReport:
        return self._report

    async def run(self, today: date | None = None) -> RunReport:
        window = self._config.window(today or date.today())
        self._report.accounts = len(self._config.account_ids)

        queue: asyncio.Queue[SourceTransaction | None] = asyncio.Queue(
            maxsize=self._config.queue_size
        )
        extract = asyncio.create_task(self._extract_all(window, queue), name="extract")
        load = asyncio.create_task(self._load_all(queue), name="load")

        try:
            done, pending = await asyncio.wait(
                {extract, load}, return_when=asyncio.FIRST_EXCEPTION
            )
            failure = next(
                (task.exception() for task in done if task.exception() is not None),
                None,
            )
            if failure is not None:
                # The sibling is very likely blocked on a full or empty queue
                # and will never observe the failure on its own.
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise failure
        except RunAborted as exc:
            self._report.aborted_reason = str(exc)
        except FatalFailure as exc:
            self._report.fatal_reason = str(exc)
        except asyncio.CancelledError:
            self._report.aborted_reason = "cancelled"
            raise
        finally:
            # Always, on every path. A run that crashed after posting two
            # hundred records must not repost them tomorrow just because the
            # crash came before a save.
            if not self._config.dry_run:
                self._checkpoint.save()
            self._report.finish()

        if self._stop.is_set() and not self._report.aborted_reason:
            self._report.aborted_reason = "interrupted before completion"

        return self._report

    # -- extract -----------------------------------------------------------

    async def _extract_all(
        self, window: DateWindow, queue: asyncio.Queue[SourceTransaction | None]
    ) -> None:
        semaphore = asyncio.Semaphore(self._config.read_concurrency)

        async def one(account_id: str) -> None:
            async with semaphore:
                await self._extract_account(account_id, window, queue)

        await asyncio.gather(*(one(a) for a in self._config.account_ids))
        # None closes the queue. Named here rather than aliased to a
        # module constant: an alias for None reads as a distinct sentinel
        # object and is not one.
        await queue.put(None)

    async def _extract_account(
        self,
        account_id: str,
        window: DateWindow,
        queue: asyncio.Queue[SourceTransaction | None],
    ) -> None:
        try:
            async for transaction in self._source.fetch(account_id, window):
                if self._stop.is_set():
                    return

                self._report.records_read += 1
                decision = decide(
                    transaction,
                    window=window,
                    is_done=self._checkpoint.is_done,
                    policy=self._config.policy,
                )
                if not decision.should_post:
                    assert decision.reason is not None
                    self._report.record_skip(decision.reason)
                    continue

                await queue.put(transaction)

        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - classified immediately below
            kind = classify_failure(exc)
            if kind is FatalFailure:
                raise FatalFailure(f"reading account {account_id}: {exc}") from exc

            # One unreadable account should not take down a run covering
            # twenty. It is recorded as a dead letter at account granularity,
            # which is enough to make the run exit non-zero and enough for
            # someone to see which account and why.
            self._report.record_dead_letter(
                DeadLetter(
                    account_id=account_id,
                    transaction_id="*",
                    stage="extract",
                    failure="terminal" if kind is TerminalFailure else "transient",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    # -- load --------------------------------------------------------------

    async def _load_all(self, queue: asyncio.Queue[SourceTransaction | None]) -> None:
        draining = False
        while True:
            item = await queue.get()
            if item is None:
                return

            if draining or self._stop.is_set():
                # Keep draining rather than returning. Extract tasks may be
                # blocked inside queue.put and would never notice the stop
                # event; pulling items lets them run far enough to see it.
                draining = True
                continue

            await self._load_one(item)

            self._since_save += 1
            if self._since_save >= self._config.checkpoint_every:
                if not self._config.dry_run:
                    self._checkpoint.save()
                self._since_save = 0

            self._check_threshold()

    async def _load_one(self, transaction: SourceTransaction) -> None:
        try:
            destination = transform(transaction)
        except TransformError as exc:
            self._report.record_dead_letter(
                DeadLetter.from_source(
                    transaction, stage="transform", failure="terminal", error=str(exc)
                )
            )
            return

        key = idempotency_key(
            source_system=self._config.source_system,
            account_id=transaction.account_id,
            transaction_id=transaction.transaction_id,
            business_date=transaction.business_date,
        )

        attempts = (
            self._config.write_retry_attempts
            if self._config.retry_writes_on_transient
            else 1
        )

        for attempt in range(1, attempts + 1):
            try:
                outcome = await self._destination.post(destination, idempotency_key=key)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - classified immediately
                kind = classify_failure(exc)

                if kind is FatalFailure:
                    raise FatalFailure(
                        f"posting {transaction.account_id}/"
                        f"{transaction.transaction_id}: {exc}"
                    ) from exc

                if kind is TransientFailure and attempt < attempts:
                    # Same key on every attempt. That is the entire reason a
                    # retry here is defensible, and it is only defensible at
                    # all because the operator explicitly enabled it after
                    # confirming the destination honors the key.
                    await self._sleep(_backoff(attempt))
                    continue

                self._report.record_dead_letter(
                    DeadLetter.from_source(
                        transaction,
                        stage="load",
                        failure=("terminal" if kind is TerminalFailure else "transient"),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                return

            self._report.posted += 1
            if outcome.deduplicated:
                self._report.deduplicated += 1

            # Only after the destination confirmed. Marking on dispatch turns
            # a lost response into a permanently skipped record.
            if not self._config.dry_run:
                self._checkpoint.mark_done(
                    transaction.account_id, transaction.transaction_id
                )
            return

    # -- threshold ---------------------------------------------------------

    def _check_threshold(self) -> None:
        report = self._report
        if report.attempted < self._config.threshold_min_attempts:
            return
        if report.failure_rate <= self._config.failure_threshold:
            return
        raise RunAborted(
            f"failure rate {report.failure_rate:.1%} exceeded threshold "
            f"{self._config.failure_threshold:.1%} after {report.attempted} "
            f"attempted writes ({report.failures} failed)"
        )


def _backoff(attempt: int, *, base: float = 0.5, cap: float = 8.0) -> float:
    """Exponential backoff with full jitter.

    Jitter matters even for a single-writer job: several accounts' worth of
    records hitting a rate limit would otherwise retry on the same cadence and
    reproduce the burst that caused it.
    """
    ceiling = min(base * (2.0 ** (attempt - 1)), cap)
    return ceiling * (0.5 + random.random() * 0.5)
