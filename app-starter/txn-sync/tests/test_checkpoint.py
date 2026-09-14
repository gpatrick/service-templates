from __future__ import annotations

import json
from pathlib import Path

import pytest

from txn_sync.checkpoint import Checkpoint


def test_load_missing_file_gives_an_empty_checkpoint(tmp_path: Path):
    cp = Checkpoint.load(tmp_path / "nope.json")
    assert len(cp) == 0
    assert not cp.is_done("acct-1", "t1")


def test_roundtrip(tmp_path: Path):
    path = tmp_path / "cp.json"
    cp = Checkpoint(path)
    cp.mark_done("acct-1", "t1")
    cp.mark_done("acct-2", "t9")
    assert cp.save() is True

    reloaded = Checkpoint.load(path)
    assert reloaded.is_done("acct-1", "t1")
    assert reloaded.is_done("acct-2", "t9")
    assert not reloaded.is_done("acct-1", "t9")
    assert len(reloaded) == 2


def test_checkpoint_is_scoped_by_account(tmp_path: Path):
    """REGRESSION. Do not delete. See test_keys for the matching case.

    Marking acct-1/t1 done must not make acct-2/t1 look done. If it does, the
    second account's transaction is skipped on every run and never crosses
    over.
    """
    cp = Checkpoint(tmp_path / "cp.json")
    cp.mark_done("acct-1", "t1")
    assert cp.is_done("acct-1", "t1")
    assert not cp.is_done("acct-2", "t1")


def test_save_is_a_noop_when_nothing_changed(tmp_path: Path):
    cp = Checkpoint(tmp_path / "cp.json")
    cp.mark_done("acct-1", "t1")
    assert cp.save() is True
    assert cp.save() is False  # unchanged
    assert cp.save(force=True) is True


def test_marking_the_same_record_twice_does_not_dirty_the_file(tmp_path: Path):
    cp = Checkpoint(tmp_path / "cp.json")
    cp.mark_done("acct-1", "t1")
    cp.save()
    cp.mark_done("acct-1", "t1")
    assert cp.dirty is False


def test_save_leaves_no_temp_files_behind(tmp_path: Path):
    path = tmp_path / "cp.json"
    cp = Checkpoint(path)
    cp.mark_done("acct-1", "t1")
    cp.save()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cp.json"]


def test_saved_file_is_sorted_so_versions_diff_cleanly(tmp_path: Path):
    path = tmp_path / "cp.json"
    cp = Checkpoint(path)
    for txn in ("t3", "t1", "t2"):
        cp.mark_done("acct-1", txn)
    cp.save()
    done = json.loads(path.read_text())["done"]
    assert done == sorted(done)


def test_corrupt_file_raises_rather_than_starting_empty(tmp_path: Path):
    """Silently starting from empty would repost the whole window.

    That may well be the right call, but it is a person's call, so the failure
    is loud and the message says what deleting the file would cost.
    """
    path = tmp_path / "cp.json"
    path.write_text("{ this is not json")
    with pytest.raises(ValueError, match="unreadable"):
        Checkpoint.load(path)


def test_unknown_format_version_raises(tmp_path: Path):
    path = tmp_path / "cp.json"
    path.write_text(json.dumps({"format_version": 99, "done": []}))
    with pytest.raises(ValueError, match="format_version"):
        Checkpoint.load(path)


def test_key_scheme_mismatch_raises(tmp_path: Path):
    """A checkpoint written under an older key scheme is not safely reusable.

    Its entries describe records whose idempotency keys have since changed, so
    skipping them no longer means what it used to mean.
    """
    path = tmp_path / "cp.json"
    path.write_text(json.dumps({"format_version": 1, "key_version": "v0", "done": []}))
    with pytest.raises(ValueError, match="key scheme"):
        Checkpoint.load(path)


def test_clear_forgets_everything(tmp_path: Path):
    cp = Checkpoint(tmp_path / "cp.json")
    cp.mark_done("acct-1", "t1")
    cp.save()
    cp.clear()
    cp.save()
    assert len(Checkpoint.load(tmp_path / "cp.json")) == 0


def test_save_creates_missing_parent_directories(tmp_path: Path):
    path = tmp_path / "nested" / "deeper" / "cp.json"
    cp = Checkpoint(path)
    cp.mark_done("acct-1", "t1")
    cp.save()
    assert path.exists()
