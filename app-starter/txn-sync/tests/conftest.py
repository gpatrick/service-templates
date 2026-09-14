from __future__ import annotations

import dataclasses
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from txn_sync.adapters.memory import InMemorySource, RecordingDestination
from txn_sync.checkpoint import Checkpoint
from txn_sync.config import SyncConfig
from txn_sync.records import DateWindow, SourceTransaction
from txn_sync.runner import SyncRunner

WINDOW = DateWindow(start=date(2026, 3, 1), end=date(2026, 3, 8))
IN_WINDOW = date(2026, 3, 3)


def make_transaction(
    transaction_id: str = "t1",
    account_id: str = "acct-1",
    *,
    business_date: date = IN_WINDOW,
    amount: str = "10.00",
    currency: str = "USD",
    kind: str | None = "purchase",
    description: str | None = "coffee",
) -> SourceTransaction:
    return SourceTransaction(
        account_id=account_id,
        transaction_id=transaction_id,
        business_date=business_date,
        amount=Decimal(amount),
        currency=currency,
        posted_at=datetime(
            business_date.year,
            business_date.month,
            business_date.day,
            12,
            0,
            tzinfo=timezone.utc,
        ),
        kind=kind,
        description=description,
        raw={"transactionId": transaction_id, "amount": amount},
    )


def make_config(tmp_path: Path, **overrides: object) -> SyncConfig:
    """A config whose window is exactly WINDOW when `today` is WINDOW.end.

    window_days=7 with end offset 0 means run(today=date(2026, 3, 8)) covers
    [2026-03-01, 2026-03-08), which is WINDOW. Tests pass that date explicitly
    rather than relying on the real clock.
    """
    base = SyncConfig(
        source_system="src",
        source_base_url="https://source.example.com",
        source_token_url="https://source.example.com/token",
        source_client_id="id",
        source_client_secret="secret",
        destination_base_url="https://dest.example.com",
        destination_token_url="https://dest.example.com/token",
        destination_client_id="id",
        destination_client_secret="secret",
        account_ids=("acct-1",),
        window_days=7,
        checkpoint_path=tmp_path / "checkpoint.json",
        dead_letter_path=tmp_path / "dead-letters.jsonl",
        # Off by default in the tests too, so a test that wants the threshold
        # has to say so and the others are not accidentally near it.
        threshold_min_attempts=1000,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


TODAY = date(2026, 3, 8)


@pytest.fixture
def checkpoint(tmp_path: Path) -> Checkpoint:
    return Checkpoint(tmp_path / "checkpoint.json")


@pytest.fixture
def config(tmp_path: Path) -> SyncConfig:
    return make_config(tmp_path)


async def noop_sleep(_: float) -> None:
    """Replaces asyncio.sleep so retry tests do not spend real seconds."""
    return None


def http_error(cls: type, status: int) -> Exception:
    request = httpx.Request("POST", "https://dest.example.com/transactions")
    response = httpx.Response(status, request=request, json={"error": "nope"})
    return cls(response)


def build(
    tmp_path: Path, transactions, destination=None, source=None, **config_overrides
):
    config = make_config(tmp_path, **config_overrides)
    source = source or InMemorySource(transactions)
    destination = destination if destination is not None else RecordingDestination()
    # load, not construct: a second call to build() against the same tmp_path
    # is what tomorrow's scheduled run looks like, and it must see yesterday's
    # resume state.
    checkpoint = Checkpoint.load(config.checkpoint_path)
    runner = SyncRunner(
        source=source,
        destination=destination,
        config=config,
        checkpoint=checkpoint,
        sleep=noop_sleep,
    )
    return runner, source, destination, checkpoint


# -- happy path ------------------------------------------------------------
