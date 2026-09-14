"""Resume state, so a re-run does not repost what already succeeded.

The checkpoint is a local file holding the identities of records the
destination has confirmed. It is an optimization, not the correctness
mechanism: the thing that actually prevents duplicates is the idempotency key
in keys.py. This file exists so a run that died three quarters of the way
through does not spend the next run's whole budget re-posting work the
destination would only deduplicate anyway.

Keeping that distinction straight matters when someone proposes deleting the
checkpoint to force a full re-sync. That is a safe operation precisely because
the keys are deterministic. If the keys were per-attempt, it would not be.

WRITES ARE ATOMIC. A job killed mid-write must not leave a truncated JSON file
that the next run fails to parse, because the failure mode is that someone
deletes the corrupt file and the whole window reposts. Write to a temp file in
the same directory, fsync the file, os.replace (atomic on POSIX and on Windows
for same-volume replacement), then fsync the directory so the rename itself is
durable rather than only the bytes.

THE FILE IS LOCAL AND PER-JOB. It is not shared state and not a lock. Two
concurrent runs of this job against the same checkpoint path will interleave
writes and lose entries. If the schedule can overlap, take a lock outside this
program, or give each run its own path.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from .keys import KEY_VERSION, checkpoint_id

__all__ = ["Checkpoint"]

_FORMAT_VERSION = 1


class Checkpoint:
    """The set of (account, transaction) pairs already confirmed by the destination.

    Construct with `Checkpoint.load(path)`, which tolerates a missing file and
    returns an empty checkpoint. A corrupt file raises, deliberately: silently
    starting from empty would repost a window, and that is a decision for a
    person to make rather than for error handling to make quietly.
    """

    def __init__(
        self,
        path: Path,
        *,
        done: set[str] | None = None,
        key_version: str = KEY_VERSION,
        created_at: str | None = None,
    ) -> None:
        self.path = path
        self._done: set[str] = set(done or ())
        self.key_version = key_version
        self.created_at = created_at or _now()
        self._dirty = False

    # -- construction ------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> Checkpoint:
        if not path.exists():
            return cls(path)

        try:
            payload = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"checkpoint at {path} is unreadable: {exc}. "
                f"Inspect it before deleting; deleting it reposts the window, "
                f"which is safe only because idempotency keys are deterministic."
            ) from exc

        version = payload.get("format_version")
        if version != _FORMAT_VERSION:
            raise ValueError(
                f"checkpoint at {path} has format_version {version!r}, "
                f"expected {_FORMAT_VERSION}"
            )

        stored_key_version = payload.get("key_version", KEY_VERSION)
        if stored_key_version != KEY_VERSION:
            # Not fatal by itself, but the operator needs to know: entries
            # recorded under an old key scheme describe records whose
            # idempotency keys have since changed, so skipping them is no
            # longer the same guarantee it was.
            raise ValueError(
                f"checkpoint at {path} was written under key scheme "
                f"{stored_key_version!r} but this build uses {KEY_VERSION!r}. "
                f"Records already posted under the old scheme can post again. "
                f"Decide on the overlap window before continuing; see keys.py."
            )

        return cls(
            path,
            done=set(payload.get("done", [])),
            key_version=stored_key_version,
            created_at=payload.get("created_at"),
        )

    # -- queries -----------------------------------------------------------

    def is_done(self, account_id: str, transaction_id: str) -> bool:
        return checkpoint_id(account_id, transaction_id) in self._done

    def __len__(self) -> int:
        return len(self._done)

    @property
    def dirty(self) -> bool:
        return self._dirty

    # -- mutation ----------------------------------------------------------

    def mark_done(self, account_id: str, transaction_id: str) -> None:
        """Record that the destination confirmed this record.

        Call this only after the destination returned successfully. Marking on
        dispatch rather than on confirmation converts a lost response into a
        permanently skipped record, which is the failure this job is least
        able to detect.
        """
        entry = checkpoint_id(account_id, transaction_id)
        if entry not in self._done:
            self._done.add(entry)
            self._dirty = True

    def extend(self, refs: Iterable[tuple[str, str]]) -> None:
        for account_id, transaction_id in refs:
            self.mark_done(account_id, transaction_id)

    # -- persistence -------------------------------------------------------

    def save(self, *, force: bool = False) -> bool:
        """Persist to disk atomically. Returns True if a write happened.

        No-ops when nothing changed, so the runner can call it on a fixed
        cadence without rewriting an unchanged file every few seconds.
        """
        if not self._dirty and not force:
            return False

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format_version": _FORMAT_VERSION,
            "key_version": self.key_version,
            "created_at": self.created_at,
            "updated_at": _now(),
            # Sorted so successive versions of the file diff cleanly. A
            # checkpoint is something people eyeball during an incident.
            "done": sorted(self._done),
        }

        # Temp file in the SAME directory: os.replace is only atomic within a
        # filesystem, and /tmp is routinely a different one.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                handle.flush()
                # Without fsync the rename can land before the bytes do, and a
                # power loss leaves a valid-looking empty file.
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
            # The file's contents are durable after the fsync above, but the
            # RENAME is not until its directory is synced too. Skipping this
            # leaves a window where a power loss reverts to the previous
            # checkpoint, which is safe but reposts a window for no reason.
            _fsync_directory(self.path.parent)
        except BaseException:
            with _suppress_oserror():
                os.unlink(tmp_name)
            raise

        self._dirty = False
        return True

    def clear(self) -> None:
        """Forget everything. The next run treats the window as unprocessed."""
        if self._done:
            self._done.clear()
            self._dirty = True


def _fsync_directory(path: Path) -> None:
    """Best effort. Not every filesystem supports syncing a directory."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:  # pragma: no cover - platform dependent
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - platform dependent
        pass
    finally:
        os.close(fd)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _suppress_oserror:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, *_: object) -> bool:
        return isinstance(exc_type, type) and issubclass(exc_type, OSError)
