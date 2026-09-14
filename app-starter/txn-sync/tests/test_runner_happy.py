"""The runner's success paths: records read, selected, posted, and recorded.

Failure handling lives in test_runner_failures.py; resume and interruption
live in test_runner_resume.py. The split is by what breaks, so a red test
name tells you which area to look at before you open anything.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from conftest import (
    TODAY,
    build,
    make_transaction,
)

from txn_sync.adapters.memory import (
    DryRunDestination,
    InMemorySource,
    RecordingDestination,
)
from txn_sync.report import ExitCode
from txn_sync.selection import SkipReason


async def test_every_in_window_record_is_posted(tmp_path: Path):
    transactions = [make_transaction(f"t{i}") for i in range(1, 4)]
    runner, _, destination, checkpoint = build(tmp_path, transactions)

    report = await runner.run(TODAY)

    assert report.records_read == 3
    assert report.posted == 3
    assert report.exit_code == ExitCode.OK
    assert destination.refs == [("acct-1", "t1"), ("acct-1", "t2"), ("acct-1", "t3")]
    assert all(checkpoint.is_done("acct-1", f"t{i}") for i in range(1, 4))


async def test_two_accounts_holding_the_same_transaction_id_both_post(
    tmp_path: Path,
):
    """REGRESSION, end to end. Do not delete.

    Source systems often number transactions per account, so acct-1/t1 and
    acct-2/t1 are different records. Keyed or checkpointed on the transaction
    id alone, the second one is either deduplicated away by the destination or
    skipped as already synced. Either way it never crosses over, and nothing
    fails visibly.
    """
    transactions = [
        make_transaction("t1", "acct-1"),
        make_transaction("t1", "acct-2"),
    ]
    runner, _, destination, checkpoint = build(
        tmp_path, transactions, account_ids=("acct-1", "acct-2")
    )

    report = await runner.run(TODAY)

    assert report.posted == 2
    assert report.deduplicated == 0
    assert sorted(destination.refs) == [("acct-1", "t1"), ("acct-2", "t1")]
    assert len(set(destination.keys)) == 2
    assert checkpoint.is_done("acct-1", "t1")
    assert checkpoint.is_done("acct-2", "t1")


# -- window ----------------------------------------------------------------


async def test_records_outside_the_window_are_skipped_even_if_the_source_sends_them(
    tmp_path: Path,
):
    """Simulates an upstream that ignores unrecognized date parameters.

    That is the realistic failure: a wrong query parameter name is usually
    ignored rather than rejected, so the call succeeds and returns everything.
    """
    transactions = [
        make_transaction("t1"),
        make_transaction("t2", business_date=date(2020, 1, 1)),
    ]
    source = InMemorySource(transactions, honor_window=False)
    runner, _, destination, _ = build(tmp_path, transactions, source=source)

    report = await runner.run(TODAY)

    assert report.posted == 1
    assert report.skipped[SkipReason.OUTSIDE_WINDOW.value] == 1
    assert destination.refs == [("acct-1", "t1")]


# -- failure handling ------------------------------------------------------


async def test_a_dry_run_posts_nothing_and_writes_no_checkpoint(tmp_path: Path):
    """A rehearsal must not make the real run skip records it never posted."""
    transactions = [make_transaction(f"t{i}") for i in range(1, 4)]
    destination = DryRunDestination()
    runner, _, _, checkpoint = build(
        tmp_path, transactions, destination=destination, dry_run=True
    )

    report = await runner.run(TODAY)

    assert report.posted == 3
    assert len(destination.would_post) == 3
    assert len(checkpoint) == 0
    assert not (tmp_path / "checkpoint.json").exists()


async def test_a_dry_run_still_derives_real_idempotency_keys(tmp_path: Path):
    """So the preview shows exactly what the real run would send."""
    destination = DryRunDestination()
    runner, _, _, _ = build(
        tmp_path, [make_transaction("t1")], destination=destination, dry_run=True
    )
    await runner.run(TODAY)
    _, key = destination.would_post[0]
    assert key.startswith("v1-")


# -- interruption ----------------------------------------------------------


async def test_read_concurrency_above_one_still_produces_serial_writes(
    tmp_path: Path,
):
    """Serial writes are what makes the failure threshold exact."""
    transactions = [
        make_transaction(f"t{i}", f"acct-{a}") for a in range(1, 5) for i in range(1, 6)
    ]

    concurrent = 0
    peak = 0

    class CountingDestination(RecordingDestination):
        async def post(self, transaction, *, idempotency_key):
            nonlocal concurrent, peak
            concurrent += 1
            peak = max(peak, concurrent)
            try:
                return await super().post(transaction, idempotency_key=idempotency_key)
            finally:
                concurrent -= 1

    destination = CountingDestination()
    runner, _, _, _ = build(
        tmp_path,
        transactions,
        destination=destination,
        account_ids=tuple(f"acct-{a}" for a in range(1, 5)),
        read_concurrency=4,
    )

    report = await runner.run(TODAY)

    assert report.posted == 20
    assert peak == 1


@pytest.mark.parametrize("queue_size", [1, 3, 200])
async def test_the_run_is_correct_at_any_queue_size(tmp_path: Path, queue_size: int):
    """A queue of 1 is maximum backpressure. Nothing should deadlock."""
    transactions = [make_transaction(f"t{i:03d}") for i in range(1, 26)]
    runner, _, destination, _ = build(tmp_path, transactions, queue_size=queue_size)

    report = await runner.run(TODAY)

    assert report.posted == 25
    assert len(destination.posted) == 25
