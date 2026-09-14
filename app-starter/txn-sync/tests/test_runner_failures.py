"""How the runner behaves when writes and reads fail.

Covers the four levels of failure handling described in errors.py: the
optional in-write retry, per-record transient and terminal outcomes, the
per-run failure threshold, and immediate stops. These are the paths least
likely to be exercised by a real integration test and most expensive to get
wrong, which is why they are tested against fakes that fail on demand.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from conftest import (
    TODAY,
    build,
    http_error,
    make_config,
    make_transaction,
    noop_sleep,
)
from service_client import (
    APIConnectionError,
    APIStatusError,
    AuthError,
    ServerError,
)

from txn_sync.adapters.memory import (
    FlakyDestination,
    InMemorySource,
    fails_n_times,
)
from txn_sync.checkpoint import Checkpoint
from txn_sync.report import ExitCode
from txn_sync.runner import SyncRunner


async def test_a_terminal_write_failure_dead_letters_and_the_run_continues(
    tmp_path: Path,
):
    destination = FlakyDestination(
        lambda txn, _: (
            http_error(APIStatusError, 422) if txn.source_transaction_id == "t2" else None
        )
    )
    transactions = [make_transaction(f"t{i}") for i in range(1, 4)]
    runner, _, _, checkpoint = build(tmp_path, transactions, destination=destination)

    report = await runner.run(TODAY)

    assert report.posted == 2
    assert report.terminal_failures == 1
    assert report.exit_code == ExitCode.COMPLETED_WITH_DEAD_LETTERS
    assert [dl.transaction_id for dl in report.dead_letters] == ["t2"]
    assert report.dead_letters[0].stage == "load"
    assert report.dead_letters[0].failure == "terminal"
    # The failed record must stay out of the checkpoint so it is reconsidered.
    assert not checkpoint.is_done("acct-1", "t2")


async def test_a_dead_letter_carries_the_full_source_payload(tmp_path: Path):
    """The point of a dead letter is to be replayed. One that only names the
    record requires going back to the source, by which time the window may
    have moved."""
    destination = FlakyDestination(lambda *_: http_error(APIStatusError, 400))
    runner, _, _, _ = build(tmp_path, [make_transaction("t1")], destination=destination)

    report = await runner.run(TODAY)

    payload = report.dead_letters[0].payload
    assert payload is not None
    assert payload["currency"] == "USD"
    assert payload["raw"]["transactionId"] == "t1"


async def test_a_transient_failure_is_not_retried_by_default(tmp_path: Path):
    """The safe default. Retrying a write is only sound once someone has
    confirmed the destination honors the idempotency key."""
    destination = FlakyDestination(
        fails_n_times(1, APIConnectionError("connection reset"))
    )
    runner, _, _, checkpoint = build(
        tmp_path, [make_transaction("t1")], destination=destination
    )

    report = await runner.run(TODAY)

    assert destination.attempts["acct-1/t1"] == 1
    assert report.posted == 0
    assert report.transient_failures == 1
    assert report.dead_letters[0].failure == "transient"
    assert not checkpoint.is_done("acct-1", "t1")


async def test_an_enabled_write_retry_reuses_the_same_idempotency_key(
    tmp_path: Path,
):
    """The only reason retrying a write is defensible at all.

    A fresh key per attempt would make the retry a new logical operation,
    which is exactly the duplicate this design exists to prevent.
    """
    seen_keys: list[str] = []

    class KeyRecordingDestination(FlakyDestination):
        async def post(self, transaction, *, idempotency_key):
            seen_keys.append(idempotency_key)
            return await super().post(transaction, idempotency_key=idempotency_key)

    destination = KeyRecordingDestination(
        fails_n_times(
            1,
            ServerError(
                httpx.Response(
                    503,
                    request=httpx.Request(
                        "POST", "https://dest.example.com/transactions"
                    ),
                    json={},
                )
            ),
        )
    )
    runner, _, _, _ = build(
        tmp_path,
        [make_transaction("t1")],
        destination=destination,
        retry_writes_on_transient=True,
        write_retry_attempts=3,
    )

    report = await runner.run(TODAY)

    assert destination.attempts["acct-1/t1"] == 2
    assert report.posted == 1
    assert len(seen_keys) == 2
    assert seen_keys[0] == seen_keys[1]


async def test_a_retry_that_never_succeeds_stops_at_the_attempt_limit(
    tmp_path: Path,
):
    destination = FlakyDestination(lambda *_: APIConnectionError("still down"))
    runner, _, _, _ = build(
        tmp_path,
        [make_transaction("t1")],
        destination=destination,
        retry_writes_on_transient=True,
        write_retry_attempts=3,
    )

    report = await runner.run(TODAY)

    assert destination.attempts["acct-1/t1"] == 3
    assert report.transient_failures == 1


async def test_an_auth_failure_stops_the_run_immediately(tmp_path: Path):
    """Bad credentials fail identically for every record. Burning the
    threshold to rediscover that wastes a run and fills the dead-letter file
    with noise."""
    destination = FlakyDestination(lambda *_: http_error(AuthError, 401))
    transactions = [make_transaction(f"t{i}") for i in range(1, 11)]
    runner, _, _, _ = build(tmp_path, transactions, destination=destination)

    report = await runner.run(TODAY)

    assert report.fatal_reason is not None
    assert report.exit_code == ExitCode.FATAL
    assert len(destination.attempts) == 1  # stopped at the first record


async def test_the_failure_threshold_aborts_the_run(tmp_path: Path):
    destination = FlakyDestination(lambda *_: http_error(APIStatusError, 422))
    transactions = [make_transaction(f"t{i}") for i in range(1, 21)]
    runner, _, _, _ = build(
        tmp_path,
        transactions,
        destination=destination,
        threshold_min_attempts=3,
        failure_threshold=0.5,
    )

    report = await runner.run(TODAY)

    assert report.aborted_reason is not None
    assert report.exit_code == ExitCode.ABORTED
    # Stopped near the floor rather than working through all twenty.
    assert report.attempted < 20


async def test_the_threshold_floor_prevents_aborting_on_the_first_failure(
    tmp_path: Path,
):
    """Without a floor, one transient blip is a 100% failure rate and every
    run with an early hiccup aborts."""
    destination = FlakyDestination(
        lambda txn, _: (
            http_error(APIStatusError, 422) if txn.source_transaction_id == "t1" else None
        )
    )
    transactions = [make_transaction(f"t{i}") for i in range(1, 11)]
    runner, _, _, _ = build(
        tmp_path,
        transactions,
        destination=destination,
        threshold_min_attempts=5,
        failure_threshold=0.5,
    )

    report = await runner.run(TODAY)

    assert report.aborted_reason is None
    assert report.posted == 9


async def test_skips_do_not_dilute_the_failure_rate(tmp_path: Path):
    """On a re-run almost everything is skipped. Counting skips as successful
    attempts would make a broken run look healthy."""
    transactions = [make_transaction(f"t{i}") for i in range(1, 11)]
    runner, _, _, checkpoint = build(tmp_path, transactions)
    await runner.run(TODAY)

    destination = FlakyDestination(lambda *_: http_error(APIStatusError, 422))
    config = make_config(tmp_path, threshold_min_attempts=1, failure_threshold=0.5)
    runner2 = SyncRunner(
        source=InMemorySource(transactions + [make_transaction("t99")]),
        destination=destination,
        config=config,
        checkpoint=Checkpoint.load(config.checkpoint_path),
        sleep=noop_sleep,
    )
    report = await runner2.run(TODAY)

    assert report.attempted == 1
    assert report.failure_rate == 1.0


async def test_a_transform_failure_is_dead_lettered_at_the_transform_stage(
    tmp_path: Path,
):
    bad = make_transaction("t2", currency="")
    transactions = [make_transaction("t1"), bad]
    runner, _, destination, _ = build(tmp_path, transactions)

    report = await runner.run(TODAY)

    assert report.posted == 1
    assert [dl.stage for dl in report.dead_letters] == ["transform"]
    assert report.dead_letters[0].transaction_id == "t2"


# -- extract-side failures -------------------------------------------------


async def test_one_unreadable_account_does_not_take_down_the_others(
    tmp_path: Path,
):
    transactions = [
        make_transaction("t1", "acct-1"),
        make_transaction("t2", "acct-2"),
    ]
    source = InMemorySource(
        transactions,
        fail_accounts={"acct-2": http_error(APIStatusError, 500)},
    )
    runner, _, destination, _ = build(
        tmp_path, transactions, source=source, account_ids=("acct-1", "acct-2")
    )

    report = await runner.run(TODAY)

    assert report.posted == 1
    assert destination.refs == [("acct-1", "t1")]
    assert [dl.account_id for dl in report.dead_letters] == ["acct-2"]
    assert report.dead_letters[0].stage == "extract"
    assert report.dead_letters[0].transaction_id == "*"


async def test_an_auth_failure_while_reading_is_fatal(tmp_path: Path):
    transactions = [make_transaction("t1", "acct-1")]
    source = InMemorySource(
        transactions, fail_accounts={"acct-1": http_error(AuthError, 403)}
    )
    runner, _, _, _ = build(tmp_path, transactions, source=source)

    report = await runner.run(TODAY)

    assert report.exit_code == ExitCode.FATAL


# -- dry run ---------------------------------------------------------------


async def test_an_unreadable_account_alone_does_not_trip_the_threshold(
    tmp_path: Path,
):
    """REGRESSION. The destination is never contacted in this run.

    Everything readable is already synced, so there are no writes at all. One
    account that cannot be read must not abort a run in which nothing was
    attempted, even with the threshold floor set as low as it goes.
    """
    transactions = [make_transaction("t1", "acct-1")]
    runner, _, destination, _ = build(
        tmp_path, transactions, account_ids=("acct-1", "acct-2")
    )
    await runner.run(TODAY)  # first run posts acct-1/t1

    source = InMemorySource(
        transactions,
        fail_accounts={"acct-2": http_error(APIStatusError, 500)},
    )
    runner2, _, destination2, _ = build(
        tmp_path,
        transactions,
        source=source,
        account_ids=("acct-1", "acct-2"),
        threshold_min_attempts=1,
        failure_threshold=0.1,
    )
    report = await runner2.run(TODAY)

    assert report.aborted_reason is None
    assert report.attempted == 0
    assert report.failure_rate == 0.0
    assert report.extract_failures == 1
    assert destination2.posted == []
