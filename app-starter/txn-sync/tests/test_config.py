from __future__ import annotations

import dataclasses
from datetime import date
from pathlib import Path

import pytest
from conftest import make_config

from txn_sync.config import load_config
from txn_sync.errors import FatalFailure

COMPLETE = {
    "SYNC_SOURCE_SYSTEM": "core-banking",
    "SYNC_SOURCE_BASE_URL": "https://source.example.com",
    "SYNC_SOURCE_TOKEN_URL": "https://source.example.com/token",
    "SYNC_SOURCE_CLIENT_ID": "id",
    "SYNC_SOURCE_CLIENT_SECRET": "secret",
    "SYNC_DEST_BASE_URL": "https://dest.example.com",
    "SYNC_DEST_TOKEN_URL": "https://dest.example.com/token",
    "SYNC_DEST_CLIENT_ID": "id",
    "SYNC_DEST_CLIENT_SECRET": "secret",
    "SYNC_ACCOUNT_IDS": "acct-1,acct-2",
}


def test_a_complete_environment_loads():
    config = load_config(COMPLETE)
    assert config.source_system == "core-banking"
    assert config.account_ids == ("acct-1", "acct-2")


def test_every_problem_is_reported_at_once():
    """Failing on the first missing variable turns configuring a scheduled job
    into a guessing loop, which on a client's machine can mean a ticket per
    cycle."""
    with pytest.raises(FatalFailure) as excinfo:
        load_config({})
    message = str(excinfo.value)
    assert "SYNC_SOURCE_BASE_URL" in message
    assert "SYNC_DEST_CLIENT_SECRET" in message
    assert "SYNC_ACCOUNT_IDS" in message


def test_the_source_system_cannot_be_defaulted():
    """It is part of every idempotency key, so a silent default would let two
    deployments generate colliding keys for records sharing an id."""
    env = dict(COMPLETE)
    del env["SYNC_SOURCE_SYSTEM"]
    with pytest.raises(FatalFailure, match="SYNC_SOURCE_SYSTEM"):
        load_config(env)


def test_account_ids_are_deduplicated_and_sorted():
    config = load_config({**COMPLETE, "SYNC_ACCOUNT_IDS": "b, a ,b,"})
    assert config.account_ids == ("a", "b")


def test_whitespace_only_values_count_as_missing():
    with pytest.raises(FatalFailure, match="SYNC_SOURCE_CLIENT_ID"):
        load_config({**COMPLETE, "SYNC_SOURCE_CLIENT_ID": "   "})


@pytest.mark.parametrize("raw,expected", [("true", True), ("0", False), ("YES", True)])
def test_booleans_accept_the_usual_spellings(raw: str, expected: bool):
    config = load_config({**COMPLETE, "SYNC_DRY_RUN": raw})
    assert config.dry_run is expected


def test_an_unparseable_boolean_is_a_problem_rather_than_a_silent_default():
    with pytest.raises(FatalFailure, match="SYNC_DRY_RUN"):
        load_config({**COMPLETE, "SYNC_DRY_RUN": "maybe"})


def test_a_non_numeric_threshold_is_rejected():
    with pytest.raises(FatalFailure, match="SYNC_FAILURE_THRESHOLD"):
        load_config({**COMPLETE, "SYNC_FAILURE_THRESHOLD": "lots"})


@pytest.mark.parametrize("raw", ["0", "-0.1", "1.5"])
def test_the_threshold_must_be_a_fraction_above_zero(raw: str):
    with pytest.raises(FatalFailure, match="SYNC_FAILURE_THRESHOLD"):
        load_config({**COMPLETE, "SYNC_FAILURE_THRESHOLD": raw})


def test_write_retries_are_off_unless_explicitly_enabled():
    """The dangerous switch. Its default is the whole point."""
    assert load_config(COMPLETE).retry_writes_on_transient is False


def test_currencies_are_uppercased_for_comparison():
    config = load_config({**COMPLETE, "SYNC_ALLOWED_CURRENCIES": "usd,eur"})
    assert config.policy.allowed_currencies == frozenset({"USD", "EUR"})


def test_redacted_output_omits_secrets():
    """This is what gets printed into a scheduler's log."""
    rendered = repr(load_config(COMPLETE).redacted())
    assert "secret" not in rendered
    assert "source.example.com" in rendered


async def test_the_window_is_derived_from_the_supplied_date(tmp_path: Path):
    config = make_config(tmp_path, window_days=2)
    window = config.window(date(2026, 3, 8))
    assert window.start == date(2026, 3, 6)
    assert window.end == date(2026, 3, 8)


async def test_an_end_offset_shifts_the_whole_window_back(tmp_path: Path):
    """For sources that only settle a day late."""
    config = dataclasses.replace(
        make_config(tmp_path), window_days=1, window_end_offset_days=1
    )
    window = config.window(date(2026, 3, 8))
    assert window.start == date(2026, 3, 6)
    assert window.end == date(2026, 3, 7)
