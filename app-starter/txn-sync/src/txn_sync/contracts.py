"""The two seams between the job's core and the outside world.

Everything in the core depends on these protocols and on nothing else. The
real HTTP implementations live in `adapters/`, the fakes used by the tests
live in `adapters/memory.py`, and neither is importable from the core. That is
what makes the pipeline testable without a network and portable to a machine
where the real systems exist.

Structural protocols rather than abstract base classes, so an implementation
does not have to import this module to satisfy it. When the client writes
their own source adapter against an internal library, it just needs the right
method signatures.

Both protocols include `aclose` because both real implementations own an HTTP
connection pool. A job that exits without closing them leaks sockets and, more
annoyingly, emits unraisable-exception warnings during interpreter shutdown
that look like real failures in a scheduler's log.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from .records import (
    DateWindow,
    DestinationTransaction,
    PostOutcome,
    SourceTransaction,
)

__all__ = ["TransactionSource", "TransactionDestination"]


@runtime_checkable
class TransactionSource(Protocol):
    """Reads transactions out of the source system."""

    def fetch(
        self, account_id: str, window: DateWindow
    ) -> AsyncIterator[SourceTransaction]:
        """Yield every transaction for one account within the window.

        Declared as a plain method returning an AsyncIterator rather than as
        an `async def`, because that is the type an async generator function
        has. Implementations are written as `async def fetch(...) -> ...:` with
        `yield` in the body.

        Streaming rather than returning a list, so that a source with a
        large window does not have to be fully materialized before the first
        record can be posted. Implementations are expected to follow
        pagination internally.

        The window is passed rather than applied by the caller so the adapter
        can push it down to the API as query parameters. Filtering after the
        fact works but drags every record in the account across the network.
        The core re-checks the window anyway; see selection.py.
        """
        ...

    async def aclose(self) -> None: ...


@runtime_checkable
class TransactionDestination(Protocol):
    """Writes transactions into the destination system."""

    async def post(
        self, transaction: DestinationTransaction, *, idempotency_key: str
    ) -> PostOutcome:
        """Create one transaction in the destination.

        `idempotency_key` is keyword-only and required. It is not optional
        because a caller who forgets it gets no error and no visible symptom,
        only duplicate records the next time a response is lost.

        Implementations MUST NOT retry internally on a non-idempotent method.
        The decision to retry a write belongs to the runner, which knows
        whether the key is actually honored by the destination; see
        `config.retry_writes_on_transient` and the warning attached to it.

        Raises whatever the underlying client raises. Classification into
        transient, terminal and fatal happens in errors.py, in one place,
        rather than being reinvented per adapter.
        """
        ...

    async def aclose(self) -> None: ...
