"""Deterministic idempotency keys. Read this module before changing anything.

This is the safety property the whole job rests on, so it is worth stating the
failure it prevents.

A scheduled job gets re-run. That re-run is a retry of every record it covers,
including the ones whose response was lost after the destination had already
applied them. The checkpoint cannot cover that case by itself: such a record is
absent from the checkpoint (we never saw a success) and present in the
destination's ledger. On the next run it is posted again.

An idempotency key closes the gap, but only if it is derived from what the
record IS rather than generated per attempt. `uuid4()` is the obvious reflex
and it is exactly wrong here: a fresh key per attempt makes every retry a new
logical operation, which is the thing we are trying to avoid. The same record
must produce the same key today, tomorrow, and after a redeploy.

SCOPED BY ACCOUNT, NOT BY TRANSACTION ID ALONE. Source systems frequently
number transactions per account rather than globally, so two accounts can each
hold a transaction called "t1". Keying on transaction id alone makes the
destination treat the second one as a repeat of the first and silently return
the original result. Money not moved, with a success in the log. There is a
regression test named for this in tests/test_keys.py; do not delete it.

BUSINESS DATE IS PART OF THE KEY. Some sources reuse transaction ids across
days. Including the date makes a same-id record on a different day a distinct
operation. If your source guarantees ids are stable and unique forever, the
date is harmless; if it does not, leaving it out is a silent skip.

KEY_VERSION exists so a change to the derivation is visible rather than
catastrophic. If you change what goes into the hash, bump it. Every record
then gets a new key, which means records already posted under the old scheme
can post a second time, so a version bump needs a plan for the overlap window,
not just a code change.
"""

from __future__ import annotations

import hashlib
from datetime import date

__all__ = ["KEY_VERSION", "idempotency_key", "checkpoint_id"]

KEY_VERSION = "v1"

# Unit separator. Chosen because it is not plausibly present in an account id,
# transaction id or ISO date. A delimiter that CAN appear in the inputs makes
# the encoding ambiguous: with a hyphen, ("ab", "c") and ("a", "bc") hash the
# same, so two different records would collide onto one key.
_SEP = "\x1f"


def idempotency_key(
    *,
    source_system: str,
    account_id: str,
    transaction_id: str,
    business_date: date,
) -> str:
    """Return the stable idempotency key for one source transaction.

    Keyword-only on purpose. Four string-ish positional arguments is an
    invitation to transpose two of them, and a transposition here produces a
    key that is wrong but perfectly well-formed, so nothing fails visibly.

    `source_system` distinguishes records that came from different upstreams
    if this job is ever pointed at a second one. It costs one config value now
    and prevents a collision that would be very hard to diagnose later.
    """
    for name, value in (
        ("source_system", source_system),
        ("account_id", account_id),
        ("transaction_id", transaction_id),
    ):
        if not value:
            raise ValueError(f"{name} is required to derive an idempotency key")

    material = _SEP.join(
        (
            KEY_VERSION,
            source_system,
            account_id,
            transaction_id,
            business_date.isoformat(),
        )
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    # Prefixed rather than bare hex so that a key appearing in a log or a
    # support ticket is self-describing, and so a scheme change is legible at
    # a glance instead of requiring someone to recompute it.
    return f"{KEY_VERSION}-{digest}"


def checkpoint_id(account_id: str, transaction_id: str) -> str:
    """Return the resume-state identity for one source transaction.

    Separate from the idempotency key because they answer different
    questions. The checkpoint asks "did THIS RUN's predecessor finish this
    record", and wants to stay readable in a file a human may open. The
    idempotency key asks "is this the same logical operation as one the
    destination may already hold", and is a hash because it is sent over the
    wire.

    Scoped by account for the same reason the key is; see the module
    docstring.
    """
    if not account_id or not transaction_id:
        raise ValueError("both account_id and transaction_id are required")
    return f"{account_id}{_SEP}{transaction_id}"
