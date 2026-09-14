"""Configuration, validated before the job makes a single network call.

Two properties matter here.

ALL PROBLEMS AT ONCE. Validation collects every complaint and raises them
together. Failing on the first missing variable turns configuring a scheduled
job into a guessing loop: fix one, re-run, discover the next. On a client's
machine, where each cycle may mean a ticket, that difference is the difference
between an afternoon and a week.

FAIL BEFORE ANY SIDE EFFECT. The job reads and validates config, then
connects. A misconfiguration that only surfaces after two hundred records have
posted is a reconciliation problem rather than an error message.

Secrets are read from the environment and never logged. `redacted()` is what
the run summary prints; it exists so that "what was this run actually pointed
at" is answerable from a scheduler log without the credentials being in it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .errors import FatalFailure
from .records import DateWindow
from .selection import SelectionPolicy

__all__ = ["SyncConfig", "load_config"]


@dataclass(frozen=True)
class SyncConfig:
    # -- source ------------------------------------------------------------
    source_system: str
    source_base_url: str
    source_token_url: str
    source_client_id: str
    source_client_secret: str

    # -- destination -------------------------------------------------------
    destination_base_url: str
    destination_token_url: str
    destination_client_id: str
    destination_client_secret: str

    # -- scope -------------------------------------------------------------
    account_ids: tuple[str, ...]
    window_days: int = 1
    window_end_offset_days: int = 0

    # -- execution ---------------------------------------------------------
    read_concurrency: int = 4
    queue_size: int = 200

    # Fraction of attempted writes that may fail before the run gives up.
    # 0.1 means one in ten. Set it low: a run that fails a third of its
    # records has a systemic problem, and continuing mostly serves to make
    # the dead-letter file longer.
    failure_threshold: float = 0.1

    # Attempts below this count are never judged against the threshold. Without
    # a floor, the first failed record in a run is a 100% failure rate and
    # every run with an early transient blip aborts.
    threshold_min_attempts: int = 20

    # THE DANGEROUS SWITCH. Leave it False unless you have CONFIRMED that the
    # destination honors the idempotency header. With it False, a write that
    # fails transiently is left for the next scheduled run, which re-derives
    # the same key. With it True and a destination that ignores the header,
    # every transient blip creates a duplicate transaction and nothing fails
    # visibly. See adapters/destination_http.py, IDEMPOTENCY_HEADER.
    retry_writes_on_transient: bool = False
    write_retry_attempts: int = 2

    # -- state -------------------------------------------------------------
    checkpoint_path: Path = Path(".txn-sync-checkpoint.json")
    dead_letter_path: Path = Path("dead-letters.jsonl")
    checkpoint_every: int = 25

    # -- behavior ----------------------------------------------------------
    dry_run: bool = False
    policy: SelectionPolicy = field(default_factory=SelectionPolicy)

    # -- derived -----------------------------------------------------------

    def window(self, today: date) -> DateWindow:
        """The business-date range this run covers.

        Half-open and anchored on `today` rather than on the previous run, so
        a run that is skipped entirely does not silently shrink the next one's
        coverage. Widen `window_days` if the schedule can miss days; the
        overlap costs nothing, because records already posted are skipped by
        the checkpoint and deduplicated by the key even if the checkpoint is
        gone.
        """
        end = today - timedelta(days=self.window_end_offset_days)
        return DateWindow(start=end - timedelta(days=self.window_days), end=end)

    def redacted(self) -> dict[str, object]:
        """Everything about this run except the secrets. Safe to log."""
        return {
            "source_system": self.source_system,
            "source_base_url": self.source_base_url,
            "destination_base_url": self.destination_base_url,
            "accounts": len(self.account_ids),
            "window_days": self.window_days,
            "window_end_offset_days": self.window_end_offset_days,
            "read_concurrency": self.read_concurrency,
            "failure_threshold": self.failure_threshold,
            "retry_writes_on_transient": self.retry_writes_on_transient,
            "checkpoint_path": str(self.checkpoint_path),
            "dead_letter_path": str(self.dead_letter_path),
            "dry_run": self.dry_run,
        }


def load_config(env: Mapping[str, str] | None = None) -> SyncConfig:
    """Build a config from environment variables, or raise FatalFailure.

    `env` is injectable so the tests do not mutate os.environ, which leaks
    between tests in ways that are miserable to track down.
    """
    env = os.environ if env is None else env
    problems: list[str] = []

    def required(name: str) -> str:
        value = (env.get(name) or "").strip()
        if not value:
            problems.append(f"{name} is required")
        return value

    def integer(name: str, default: int, *, minimum: int = 1) -> int:
        raw = (env.get(name) or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            problems.append(f"{name} must be an integer, got {raw!r}")
            return default
        if value < minimum:
            problems.append(f"{name} must be at least {minimum}, got {value}")
            return default
        return value

    def fraction(name: str, default: float) -> float:
        raw = (env.get(name) or "").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            problems.append(f"{name} must be a number between 0 and 1, got {raw!r}")
            return default
        if not 0.0 < value <= 1.0:
            problems.append(f"{name} must be greater than 0 and at most 1, got {value}")
            return default
        return value

    def boolean(name: str, default: bool) -> bool:
        raw = (env.get(name) or "").strip().lower()
        if not raw:
            return default
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        problems.append(f"{name} must be a boolean, got {raw!r}")
        return default

    def csv_set(name: str) -> frozenset[str]:
        raw = (env.get(name) or "").strip()
        if not raw:
            return frozenset()
        return frozenset(part.strip() for part in raw.split(",") if part.strip())

    source_system = (env.get("SYNC_SOURCE_SYSTEM") or "").strip()
    if not source_system:
        # Part of every idempotency key, so it cannot be defaulted silently.
        # A default here would mean two deployments pointed at different
        # sources generate colliding keys for records that share an id.
        problems.append(
            "SYNC_SOURCE_SYSTEM is required; it is part of every idempotency "
            "key, so it must be stable and distinct per source system"
        )

    account_ids = tuple(sorted(csv_set("SYNC_ACCOUNT_IDS")))
    if not account_ids:
        problems.append("SYNC_ACCOUNT_IDS is required (comma separated)")

    config_kwargs = dict(
        source_system=source_system,
        source_base_url=required("SYNC_SOURCE_BASE_URL"),
        source_token_url=required("SYNC_SOURCE_TOKEN_URL"),
        source_client_id=required("SYNC_SOURCE_CLIENT_ID"),
        source_client_secret=required("SYNC_SOURCE_CLIENT_SECRET"),
        destination_base_url=required("SYNC_DEST_BASE_URL"),
        destination_token_url=required("SYNC_DEST_TOKEN_URL"),
        destination_client_id=required("SYNC_DEST_CLIENT_ID"),
        destination_client_secret=required("SYNC_DEST_CLIENT_SECRET"),
        account_ids=account_ids,
        window_days=integer("SYNC_WINDOW_DAYS", 1),
        window_end_offset_days=integer("SYNC_WINDOW_END_OFFSET_DAYS", 0, minimum=0),
        read_concurrency=integer("SYNC_READ_CONCURRENCY", 4),
        queue_size=integer("SYNC_QUEUE_SIZE", 200),
        failure_threshold=fraction("SYNC_FAILURE_THRESHOLD", 0.1),
        threshold_min_attempts=integer("SYNC_THRESHOLD_MIN_ATTEMPTS", 20, minimum=0),
        retry_writes_on_transient=boolean("SYNC_RETRY_WRITES_ON_TRANSIENT", False),
        write_retry_attempts=integer("SYNC_WRITE_RETRY_ATTEMPTS", 2),
        checkpoint_path=Path(
            (env.get("SYNC_CHECKPOINT_PATH") or ".txn-sync-checkpoint.json").strip()
        ),
        dead_letter_path=Path(
            (env.get("SYNC_DEAD_LETTER_PATH") or "dead-letters.jsonl").strip()
        ),
        checkpoint_every=integer("SYNC_CHECKPOINT_EVERY", 25),
        dry_run=boolean("SYNC_DRY_RUN", False),
        policy=SelectionPolicy(
            allowed_currencies=frozenset(
                c.upper() for c in csv_set("SYNC_ALLOWED_CURRENCIES")
            ),
            excluded_kinds=csv_set("SYNC_EXCLUDED_KINDS"),
            skip_zero_amount=boolean("SYNC_SKIP_ZERO_AMOUNT", True),
        ),
    )

    if problems:
        raise FatalFailure(
            "configuration is incomplete:\n  "
            + "\n  ".join(sorted(problems))
            + "\n\nSee .env.example for the full list."
        )

    return SyncConfig(**config_kwargs)  # type: ignore[arg-type]
