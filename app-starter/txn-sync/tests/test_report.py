from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import make_transaction

from txn_sync.report import DeadLetter, ExitCode, RunReport, write_dead_letters
from txn_sync.selection import SkipReason


def letter(failure: str = "terminal") -> DeadLetter:
    return DeadLetter.from_source(
        make_transaction("t1"), stage="load", failure=failure, error="boom"
    )


def test_a_clean_run_exits_zero():
    report = RunReport()
    report.posted = 5
    assert report.exit_code == ExitCode.OK


def test_dead_letters_produce_exit_one():
    report = RunReport()
    report.record_dead_letter(letter())
    assert report.exit_code == ExitCode.COMPLETED_WITH_DEAD_LETTERS


def test_an_abort_produces_exit_two():
    report = RunReport()
    report.record_dead_letter(letter())
    report.aborted_reason = "threshold"
    assert report.exit_code == ExitCode.ABORTED


def test_a_fatal_error_produces_exit_three_and_outranks_everything():
    """The codes must not overlap: a scheduler alerting on 3 should not have
    that masked by the run also having dead letters."""
    report = RunReport()
    report.record_dead_letter(letter())
    report.aborted_reason = "threshold"
    report.fatal_reason = "bad credentials"
    assert report.exit_code == ExitCode.FATAL


def test_skips_are_not_counted_as_attempts():
    report = RunReport()
    report.posted = 2
    for _ in range(10):
        report.record_skip(SkipReason.ALREADY_SYNCED)
    report.record_dead_letter(letter())
    assert report.attempted == 3
    assert report.failure_rate == 1 / 3


def test_failure_rate_is_zero_when_nothing_was_attempted():
    assert RunReport().failure_rate == 0.0


def test_transient_and_terminal_failures_are_counted_separately():
    report = RunReport()
    report.record_dead_letter(letter("terminal"))
    report.record_dead_letter(letter("transient"))
    assert report.terminal_failures == 1
    assert report.transient_failures == 1
    assert report.failures == 2


def test_dead_letters_are_appended_not_overwritten(tmp_path: Path):
    """A record that fails every day should leave a growing trail, not a file
    that always looks like it holds one problem."""
    path = tmp_path / "dl.jsonl"
    write_dead_letters([letter()], path)
    write_dead_letters([letter()], path)
    assert len(path.read_text().strip().splitlines()) == 2


def test_each_dead_letter_is_one_self_contained_json_line(tmp_path: Path):
    path = tmp_path / "dl.jsonl"
    write_dead_letters([letter()], path)
    parsed = json.loads(path.read_text().strip())
    assert parsed["account_id"] == "acct-1"
    assert parsed["transaction_id"] == "t1"
    assert parsed["stage"] == "load"
    assert parsed["payload"]["raw"]["transactionId"] == "t1"


def test_amounts_serialize_as_strings_not_floats(tmp_path: Path):
    """A dead letter exists to be replayed, and a round trip through binary
    floating point can change the amount."""
    path = tmp_path / "dl.jsonl"
    source = make_transaction("t1", amount="10.10")
    write_dead_letters(
        [DeadLetter.from_source(source, stage="load", failure="terminal", error="x")],
        path,
    )
    parsed = json.loads(path.read_text().strip())
    assert parsed["payload"]["amount"] == "10.10"
    assert Decimal(parsed["payload"]["amount"]) == Decimal("10.10")


def test_dates_serialize_as_iso_strings(tmp_path: Path):
    path = tmp_path / "dl.jsonl"
    write_dead_letters([letter()], path)
    parsed = json.loads(path.read_text().strip())
    assert parsed["payload"]["business_date"] == "2026-03-03"


def test_writing_no_dead_letters_creates_no_file(tmp_path: Path):
    path = tmp_path / "dl.jsonl"
    assert write_dead_letters([], path) == 0
    assert not path.exists()


def test_the_summary_names_every_skip_reason():
    report = RunReport()
    report.record_skip(SkipReason.ALREADY_SYNCED)
    report.record_skip(SkipReason.OUTSIDE_WINDOW)
    rendered = report.render()
    assert "already_synced" in rendered
    assert "outside_window" in rendered


def test_the_summary_marks_a_dry_run():
    assert "DRY RUN" in RunReport(dry_run=True).render()


def test_the_exit_codes_are_all_distinct():
    """They are the job's only interface to the scheduler, so an accidental
    collision makes two different situations alert identically."""
    codes = [
        ExitCode.OK,
        ExitCode.COMPLETED_WITH_DEAD_LETTERS,
        ExitCode.ABORTED,
        ExitCode.FATAL,
        ExitCode.OUTPUT_UNWRITABLE,
    ]
    assert len(set(codes)) == len(codes)


def test_writing_dead_letters_raises_when_the_path_is_unwritable(tmp_path: Path):
    """The caller has to catch this. An escaping OSError exits 1, which is
    indistinguishable from 'completed with dead letters' and gets the opposite
    response from the one it needs."""
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("x")
    with pytest.raises(OSError):
        write_dead_letters([letter()], not_a_directory / "dl.jsonl")


def extract_letter() -> DeadLetter:
    return DeadLetter(
        account_id="acct-2",
        transaction_id="*",
        stage="extract",
        failure="terminal",
        error="could not read account",
    )


def test_an_unread_account_is_not_a_write_attempt():
    """REGRESSION. Found by an end-to-end run, not by this suite.

    An account that could not be READ produces no request to the destination.
    Counting it as a failed attempt meant a re-run where everything else was
    already synced reported a 100% failure rate over one attempt, with the
    destination never contacted.
    """
    report = RunReport()
    report.record_dead_letter(extract_letter())
    assert report.attempted == 0
    assert report.failure_rate == 0.0
    assert report.failures == 1  # still visible, still exits non-zero
    assert report.exit_code == ExitCode.COMPLETED_WITH_DEAD_LETTERS


def test_write_failures_and_extract_failures_are_counted_separately():
    report = RunReport()
    report.posted = 3
    report.record_dead_letter(letter("terminal"))
    report.record_dead_letter(extract_letter())
    assert report.failures == 2
    assert report.write_failures == 1
    assert report.extract_failures == 1
    assert report.attempted == 4
    assert report.failure_rate == 0.25


def test_the_summary_reports_unread_accounts_separately_from_failed_writes():
    report = RunReport()
    report.posted = 1
    report.record_dead_letter(extract_letter())
    rendered = report.render()
    assert "unread" in rendered
