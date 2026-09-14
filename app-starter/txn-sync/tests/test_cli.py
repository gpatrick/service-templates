"""Tests for the command line entry point.

Exit codes are the job's interface to the scheduler, so they are worth testing
directly rather than trusting that report.exit_code reaches the process exit
intact. These cover the paths that fail before any network call; the paths
that need a source and a destination are covered in test_runner.py against the
in-memory fakes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from txn_sync.report import ExitCode

COMPLETE_ENV = {
    "SYNC_SOURCE_SYSTEM": "core",
    "SYNC_SOURCE_BASE_URL": "https://source.invalid",
    "SYNC_SOURCE_TOKEN_URL": "https://source.invalid/token",
    "SYNC_SOURCE_CLIENT_ID": "id",
    "SYNC_SOURCE_CLIENT_SECRET": "secret",
    "SYNC_DEST_BASE_URL": "https://dest.invalid",
    "SYNC_DEST_TOKEN_URL": "https://dest.invalid/token",
    "SYNC_DEST_CLIENT_ID": "id",
    "SYNC_DEST_CLIENT_SECRET": "secret",
    "SYNC_ACCOUNT_IDS": "acct-1",
}


def run_cli(*args: str, env: dict[str, str] | None = None):
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "txn_sync", *args],
        capture_output=True,
        text=True,
        env=environment,
    )


def test_missing_configuration_exits_fatal_and_names_every_problem():
    result = run_cli(env={})
    assert result.returncode == ExitCode.FATAL
    assert "SYNC_SOURCE_BASE_URL" in result.stderr
    assert "SYNC_ACCOUNT_IDS" in result.stderr


def test_a_malformed_as_of_date_exits_fatal():
    result = run_cli("--as-of", "March 3rd", env=COMPLETE_ENV)
    assert result.returncode == ExitCode.FATAL
    assert "--as-of" in result.stderr


def test_an_empty_accounts_override_exits_fatal():
    result = run_cli("--accounts", " , ", env=COMPLETE_ENV)
    assert result.returncode == ExitCode.FATAL


def test_a_zero_window_exits_fatal():
    result = run_cli("--window-days", "0", env=COMPLETE_ENV)
    assert result.returncode == ExitCode.FATAL


def test_help_works_without_any_configuration():
    """So someone can discover the flags before the credentials exist."""
    result = run_cli("--help", env={})
    assert result.returncode == 0
    assert "--dry-run" in result.stdout


def test_the_redacted_config_reaches_stderr_without_secrets():
    """It goes to stderr so a scheduler capturing stdout gets only the summary."""
    result = run_cli("--as-of", "nope", env=COMPLETE_ENV)
    assert "source.invalid" in result.stderr
    assert "secret" not in result.stderr


def test_an_unwritable_dead_letter_path_exits_four_and_dumps_to_stderr(
    tmp_path: Path, monkeypatch, capsys
):
    """The run succeeded; only its record could not be persisted.

    Exit 4 rather than 1, because a 1 tells the operator to go read a file that
    does not exist. The letters go to stderr so the journal still holds them.
    """
    from txn_sync import __main__ as cli
    from txn_sync.report import DeadLetter, RunReport

    report = RunReport()
    report.record_dead_letter(
        DeadLetter(
            account_id="acct-1",
            transaction_id="t1",
            stage="load",
            failure="terminal",
            error="boom",
        )
    )

    async def fake_run(config, today, reset):
        return report

    blocker = tmp_path / "file"
    blocker.write_text("x")

    monkeypatch.setattr(cli, "_run", fake_run)
    for key, value in COMPLETE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SYNC_DEAD_LETTER_PATH", str(blocker / "dl.jsonl"))

    assert cli.main([]) == ExitCode.OUTPUT_UNWRITABLE
    assert '"transaction_id": "t1"' in capsys.readouterr().err


def test_a_run_with_writable_state_still_exits_one_for_dead_letters(
    tmp_path: Path, monkeypatch
):
    """The control for the test above: same report, writable path, exit 1."""
    from txn_sync import __main__ as cli
    from txn_sync.report import DeadLetter, RunReport

    report = RunReport()
    report.record_dead_letter(
        DeadLetter(
            account_id="acct-1",
            transaction_id="t1",
            stage="load",
            failure="terminal",
            error="boom",
        )
    )

    async def fake_run(config, today, reset):
        return report

    monkeypatch.setattr(cli, "_run", fake_run)
    for key, value in COMPLETE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SYNC_DEAD_LETTER_PATH", str(tmp_path / "dl.jsonl"))

    assert cli.main([]) == ExitCode.COMPLETED_WITH_DEAD_LETTERS
    assert (tmp_path / "dl.jsonl").exists()


def test_a_dry_run_does_not_append_to_the_dead_letter_file(
    tmp_path: Path, monkeypatch, capsys
):
    """REGRESSION. Found by an end-to-end run.

    The dead-letter file is append-only and read during incidents, so a line
    written by a rehearsal is indistinguishable from a real failure. The
    letters still reach stderr, where they are visible without being recorded.
    """
    from txn_sync import __main__ as cli
    from txn_sync.report import DeadLetter, RunReport

    report = RunReport(dry_run=True)
    report.record_dead_letter(
        DeadLetter(
            account_id="acct-1",
            transaction_id="t1",
            stage="load",
            failure="terminal",
            error="boom",
        )
    )

    async def fake_run(config, today, reset):
        return report

    path = tmp_path / "dl.jsonl"
    monkeypatch.setattr(cli, "_run", fake_run)
    for key, value in COMPLETE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SYNC_DEAD_LETTER_PATH", str(path))

    assert cli.main(["--dry-run"]) == ExitCode.COMPLETED_WITH_DEAD_LETTERS
    assert not path.exists()
    assert '"transaction_id": "t1"' in capsys.readouterr().err
