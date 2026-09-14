"""The plain data shapes the job's core works with.

These are deliberately NOT the source system's models and NOT the destination
system's models. They are the narrow middle that both adapters translate to
and from, and they exist so that `selection.py`, `transform.py`, `keys.py` and
`runner.py` can be written, read and tested without either proprietary API
being present.

That indirection earns its keep in three places:

  * The whole pipeline is testable with in-memory fakes, so retry behavior,
    resume behavior and the failure threshold get real coverage here rather
    than being discovered against the client's systems.
  * When the real field names arrive, the change is confined to the two
    adapter modules. Nothing in the core moves.
  * The two systems version independently. A rename on one side does not
    ripple into code that deals with the other.

`raw` is the escape hatch. Adapters put the complete decoded upstream payload
there, so a destination field with no modelled source analogue can still be
reached from the transform without widening this dataclass first. Prefer a
named field once you know a value is genuinely required; `raw` lookups are
untyped and fail at runtime rather than at import.

MONEY IS Decimal, NEVER float. Binary floating point cannot represent most
decimal amounts exactly and the error compounds across a summed batch. The
adapters are responsible for constructing Decimal from whatever the upstream
sends, including the minor-units case where an integer 1234 means 12.34.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

__all__ = [
    "SourceTransaction",
    "DestinationTransaction",
    "DateWindow",
    "PostOutcome",
]


@dataclass(frozen=True)
class PostOutcome:
    """What the destination did with a posted transaction.

    Lives here with the other plain data shapes rather than next to the
    protocol that returns it. It has no behavior and no dependency on the
    protocol, and splitting the data shapes across two modules meant that
    someone looking for "the record types" found two of the three.

    `deduplicated` is what an idempotency key buys and it is worth surfacing
    rather than folding into success. A run whose records are nearly all
    deduplicated is telling you something: usually that the checkpoint was
    lost, occasionally that the schedule is firing twice. Both are worth
    seeing in the summary rather than inferring from timing.

    Destinations that do not report deduplication leave it False, which is the
    honest answer: we do not know. Do not infer it from a 200 versus a 201
    without confirming that the destination actually distinguishes them.
    """

    destination_id: str | None = None
    deduplicated: bool = False


@dataclass(frozen=True)
class SourceTransaction:
    """One transaction as read from the source system.

    Frozen because the pipeline treats extracted records as immutable facts.
    A transform that needs to change something returns a new
    DestinationTransaction rather than mutating the input, which keeps the
    dead-letter payload faithful to what was actually read.

    account_id is the source system's grouping key. It is called an account
    here because that is what it is in the reference adapter, but the only
    property the core relies on is that it partitions transaction ids: ids
    need to be unique WITHIN an account, not globally. Everything downstream
    is scoped by the pair, so a source that reuses ids across accounts is
    handled correctly. See keys.py and checkpoint.py.
    """

    account_id: str
    transaction_id: str
    business_date: date
    amount: Decimal
    currency: str
    posted_at: datetime | None = None
    kind: str | None = None
    description: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> tuple[str, str]:
        """The identity of this record: account plus transaction id.

        Use this anywhere a record needs naming. Passing transaction_id alone
        is the mistake this property exists to make inconvenient.
        """
        return (self.account_id, self.transaction_id)


@dataclass(frozen=True)
class DestinationTransaction:
    """One transaction as it will be posted to the destination system.

    `source_transaction_id` is carried through deliberately. If the
    destination can store an external reference, populate it: reconciling the
    two systems afterwards is otherwise a fuzzy match on amount and date, and
    a lost response leaves a record you cannot look up.

    `extra` holds destination fields that have no source analogue, such as a
    ledger code or a posting channel. Fill it in transform.py so the values
    stay visible in one place instead of being scattered through the destination
    adapter.
    """

    account_id: str
    source_transaction_id: str
    business_date: date
    amount: Decimal
    currency: str
    description: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DateWindow:
    """An inclusive-start, exclusive-end range of business dates.

    Half-open on purpose. Inclusive-end ranges make consecutive runs either
    overlap by a day or skip one, depending on which mistake you make, and
    both are quiet. With a half-open window, yesterday's `end` is today's
    `start` and neither problem is expressible.
    """

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"window end {self.end} precedes start {self.start}")

    def contains(self, when: date) -> bool:
        return self.start <= when < self.end

    def __str__(self) -> str:
        return f"[{self.start.isoformat()}, {self.end.isoformat()})"
