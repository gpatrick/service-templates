"""Which records to post, and why the others were skipped. No I/O here.

Every decision this job makes about an individual record is a pure function of
the record plus a little configuration. Keeping them here, separate from the
orchestration in runner.py, has one concrete payoff: the interesting cases are
unit tests over plain values rather than integration tests that need a fake
source, a fake destination and an event loop.

A skip is a first-class outcome with a named reason, not a silent `continue`.
The reasons are what the operator reads when the summary says 400 read, 12
posted. Without them the only way to find out where the other 388 went is to
add logging and wait for tomorrow's run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .records import DateWindow, SourceTransaction

__all__ = ["Action", "SkipReason", "Decision", "decide", "SelectionPolicy"]


class Action(Enum):
    POST = "post"
    SKIP = "skip"


class SkipReason(str, Enum):
    """Named reasons, so the run summary can be read without the source.

    str-valued so they serialize into the report and the dead-letter file
    without a custom encoder.
    """

    ALREADY_SYNCED = "already_synced"
    OUTSIDE_WINDOW = "outside_window"
    ZERO_AMOUNT = "zero_amount"
    UNSUPPORTED_CURRENCY = "unsupported_currency"
    EXCLUDED_KIND = "excluded_kind"


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: SkipReason | None = None

    @property
    def should_post(self) -> bool:
        return self.action is Action.POST


POST = Decision(Action.POST)


@dataclass(frozen=True)
class SelectionPolicy:
    """The configurable half of the rules. CUSTOMIZE THE DEFAULTS.

    These defaults are intentionally permissive: everything in the window
    that is not already synced and not zero gets posted. That is the right
    starting point for a system whose semantics you do not yet know, because
    the alternative failure, silently dropping records that should have moved,
    is much harder to notice than posting something the destination rejects.

    `allowed_currencies` empty means all currencies pass. Populate it once you
    know what the destination actually accepts; a currency it cannot store
    otherwise becomes a terminal failure per record rather than a clean skip.

    `excluded_kinds` is for source record types that must not cross over at
    all. Pending authorizations and memo-only lines are the usual candidates.
    Get this list from the client rather than guessing: the cost of guessing
    wrong in either direction is a reconciliation problem.
    """

    allowed_currencies: frozenset[str] = frozenset()
    excluded_kinds: frozenset[str] = frozenset()
    skip_zero_amount: bool = True


def decide(
    transaction: SourceTransaction,
    *,
    window: DateWindow,
    is_done: Callable[[str, str], bool],
    policy: SelectionPolicy | None = None,
) -> Decision:
    """Return whether to post this record, or why it is being skipped.

    `is_done` is injected rather than the Checkpoint being imported, so this
    module has no dependency on persistence and the tests can pass a lambda.

    Order matters. ALREADY_SYNCED is checked first because it is the common
    case on a re-run and because a record that has already crossed over should
    report as such even if a later rule would also have excluded it: seeing
    `zero_amount` for something the destination already holds would send
    someone looking for a bug that is not there.
    """
    policy = policy or SelectionPolicy()

    if is_done(transaction.account_id, transaction.transaction_id):
        return Decision(Action.SKIP, SkipReason.ALREADY_SYNCED)

    # Re-checked here even though the adapter pushes the window down to the
    # API as query parameters. An unrecognized query parameter is usually
    # ignored rather than rejected, so a wrong parameter name upstream fails
    # quietly and returns the whole history looking perfectly plausible. This
    # check is what turns that into a pile of OUTSIDE_WINDOW skips in the
    # summary instead of a year of transactions posted to the destination.
    if not window.contains(transaction.business_date):
        return Decision(Action.SKIP, SkipReason.OUTSIDE_WINDOW)

    if policy.skip_zero_amount and transaction.amount == Decimal("0"):
        return Decision(Action.SKIP, SkipReason.ZERO_AMOUNT)

    if (
        policy.allowed_currencies
        and transaction.currency not in policy.allowed_currencies
    ):
        return Decision(Action.SKIP, SkipReason.UNSUPPORTED_CURRENCY)

    if transaction.kind is not None and transaction.kind in policy.excluded_kinds:
        return Decision(Action.SKIP, SkipReason.EXCLUDED_KIND)

    return POST
