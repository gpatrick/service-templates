"""Resume state across runs, and what survives an interrupted one.

A scheduled job's retry mechanism is the next run, so these tests are about
whether that next run does the right amount of work: not reposting what
succeeded, and not skipping what did not.
"""

from __future__ import annotations

from pathlib import Path

from conftest import (
    TODAY,
    build,
    http_error,
    make_transaction,
)
from service_client import (
    AuthError,
)

from txn_sync.adapters.memory import (
    FlakyDestination,
    RecordingDestination,
)
from txn_sync.checkpoint import Checkpoint
from txn_sync.selection import SkipReason


async def test_the_checkpoint_is_persisted(tmp_path: Path):
    runner, _, _, _ = build(tmp_path, [make_transaction("t1")])
    await runner.run(TODAY)
    reloaded = Checkpoint.load(tmp_path / "checkpoint.json")
    assert reloaded.is_done("acct-1", "t1")


async def test_a_second_run_skips_what_the_first_posted(tmp_path: Path):
    transactions = [make_transaction("t1"), make_transaction("t2")]
    runner, _, destination, _ = build(tmp_path, transactions)
    await runner.run(TODAY)

    # Same checkpoint file, fresh runner and destination: this is what tomorrow's
    # scheduled run looks like when the window still overlaps.
    runner2, _, destination2, _ = build(tmp_path, transactions)
    report = await runner2.run(TODAY)

    assert report.posted == 0
    assert report.skipped[SkipReason.ALREADY_SYNCED.value] == 2
    assert destination2.posted == []


# -- the multi-account regression -----------------------------------------


async def test_the_checkpoint_survives_a_fatal_error(tmp_path: Path):
    """Records that already crossed over must not repost tomorrow just
    because the run later hit something fatal."""
    destination = FlakyDestination(
        lambda txn, _: (
            http_error(AuthError, 401) if txn.source_transaction_id == "t3" else None
        )
    )
    transactions = [make_transaction(f"t{i}") for i in range(1, 6)]
    runner, _, _, _ = build(tmp_path, transactions, destination=destination)

    await runner.run(TODAY)

    reloaded = Checkpoint.load(tmp_path / "checkpoint.json")
    assert reloaded.is_done("acct-1", "t1")
    assert reloaded.is_done("acct-1", "t2")
    assert not reloaded.is_done("acct-1", "t3")


async def test_records_are_checkpointed_periodically_during_a_long_run(
    tmp_path: Path,
):
    """A crash halfway through must not throw away the first half."""
    transactions = [make_transaction(f"t{i:03d}") for i in range(1, 11)]
    runner, _, _, _ = build(tmp_path, transactions, checkpoint_every=2)

    await runner.run(TODAY)

    assert len(Checkpoint.load(tmp_path / "checkpoint.json")) == 10


async def test_a_stop_request_ends_the_run_and_keeps_what_succeeded(
    tmp_path: Path,
):
    transactions = [make_transaction(f"t{i:03d}") for i in range(1, 51)]

    class StopAfterThree(RecordingDestination):
        async def post(self, transaction, *, idempotency_key):
            outcome = await super().post(transaction, idempotency_key=idempotency_key)
            if len(self.posted) == 3:
                runner.request_stop()
            return outcome

    destination = StopAfterThree()
    runner, _, _, checkpoint = build(tmp_path, transactions, destination=destination)

    report = await runner.run(TODAY)

    assert report.posted == 3
    assert report.aborted_reason == "interrupted before completion"
    assert len(checkpoint) == 3
    assert Checkpoint.load(tmp_path / "checkpoint.json").is_done("acct-1", "t001")


# -- configuration knobs ---------------------------------------------------
