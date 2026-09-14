"""txn-sync: a scheduled job that copies transactions between two systems.

Read these in order to understand the job:

    records.py      the two record shapes everything else is written against
    contracts.py        the seams between the core and the two proprietary systems
    keys.py         deterministic idempotency keys; the safety property
    selection.py        which records are posted and why the rest are skipped
    transform.py    source shape to destination shape; the main fill-in point
    runner.py       orchestration and the four levels of failure handling

The core imports no adapter and no HTTP client. That is what lets the whole
pipeline run against in-memory fakes, and it is the property to preserve when
changing anything here.
"""

from .config import SyncConfig, load_config
from .contracts import TransactionDestination, TransactionSource
from .errors import FatalFailure, RunAborted, SyncError, TerminalFailure, TransientFailure
from .keys import KEY_VERSION, checkpoint_id, idempotency_key
from .records import (
    DateWindow,
    DestinationTransaction,
    PostOutcome,
    SourceTransaction,
)
from .report import DeadLetter, ExitCode, RunReport
from .runner import SyncRunner

__version__ = "0.1.0"

__all__ = [
    "KEY_VERSION",
    "RunAborted",
    "DateWindow",
    "DeadLetter",
    "DestinationTransaction",
    "ExitCode",
    "FatalFailure",
    "PostOutcome",
    "RunReport",
    "SourceTransaction",
    "SyncConfig",
    "SyncError",
    "SyncRunner",
    "TerminalFailure",
    "TransactionDestination",
    "TransactionSource",
    "TransientFailure",
    "checkpoint_id",
    "idempotency_key",
    "load_config",
]
