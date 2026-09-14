"""In-memory implementations of both ports.

These are shipped in `src/`, not in `tests/`, on purpose. Two reasons.

The dry-run mode uses `DryRunDestination` in production, so it has to be importable
from the installed package. And the client's team will need these to test
whatever they build on top of this job; a fake that lives in someone else's
test directory is a fake nobody else can use.

`FlakyDestination` is the interesting one. The retry, threshold, dead-letter and
resume behaviors are the parts of this job most likely to be wrong and least
likely to be exercised by a happy-path integration test, so they are tested
here against a destination that fails on demand rather than against a real
system on a bad day.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable, Sequence

from ..records import (
    DateWindow,
    DestinationTransaction,
    PostOutcome,
    SourceTransaction,
)

__all__ = [
    "InMemorySource",
    "RecordingDestination",
    "FlakyDestination",
    "DryRunDestination",
    "fails_n_times",
]


class InMemorySource:
    """Serves a fixed set of transactions, grouped by account.

    Applies the window itself, the way a real adapter would by pushing it
    down to the API as query parameters. Tests that want to prove the core
    re-checks the window can pass `honor_window=False` to simulate an upstream
    that ignores an unrecognized date parameter, which is the realistic
    failure: unknown query parameters are usually ignored, not rejected.
    """

    def __init__(
        self,
        transactions: Iterable[SourceTransaction],
        *,
        honor_window: bool = True,
        fail_accounts: dict[str, Exception] | None = None,
    ) -> None:
        self._by_account: dict[str, list[SourceTransaction]] = {}
        for transaction in transactions:
            self._by_account.setdefault(transaction.account_id, []).append(transaction)
        self._honor_window = honor_window
        self._fail_accounts = dict(fail_accounts or {})
        self.closed = False
        self.fetched: list[str] = []

    async def fetch(
        self, account_id: str, window: DateWindow
    ) -> AsyncIterator[SourceTransaction]:
        self.fetched.append(account_id)
        failure = self._fail_accounts.get(account_id)
        if failure is not None:
            raise failure
        for transaction in self._by_account.get(account_id, []):
            if self._honor_window and not window.contains(transaction.business_date):
                continue
            yield transaction

    async def aclose(self) -> None:
        self.closed = True


class RecordingDestination:
    """Accepts everything and remembers what it was given.

    Deduplicates on the idempotency key, because that is what a correctly
    implemented destination does, and a fake that does not would let a
    duplicate-producing bug pass the tests.
    """

    def __init__(self) -> None:
        self.posted: list[tuple[DestinationTransaction, str]] = []
        self._seen: dict[str, str] = {}
        self.closed = False

    async def post(
        self, transaction: DestinationTransaction, *, idempotency_key: str
    ) -> PostOutcome:
        if idempotency_key in self._seen:
            return PostOutcome(
                destination_id=self._seen[idempotency_key], deduplicated=True
            )
        destination_id = f"dest-{len(self.posted) + 1}"
        self._seen[idempotency_key] = destination_id
        self.posted.append((transaction, idempotency_key))
        return PostOutcome(destination_id=destination_id)

    @property
    def keys(self) -> list[str]:
        return [key for _, key in self.posted]

    @property
    def refs(self) -> list[tuple[str, str]]:
        """(account_id, source_transaction_id) for everything accepted."""
        return [(t.account_id, t.source_transaction_id) for t, _ in self.posted]

    async def aclose(self) -> None:
        self.closed = True


class FlakyDestination(RecordingDestination):
    """Fails according to a rule, then behaves like RecordingDestination.

    `failure_for` receives the transaction and the attempt number for that
    transaction, and returns an exception to raise or None to accept. Passing
    the attempt number is what makes "fails once then succeeds" expressible,
    which is the case the write-retry path exists for.
    """

    def __init__(
        self,
        failure_for: Callable[[DestinationTransaction, int], Exception | None],
    ) -> None:
        super().__init__()
        self._failure_for = failure_for
        self.attempts: dict[str, int] = {}

    async def post(
        self, transaction: DestinationTransaction, *, idempotency_key: str
    ) -> PostOutcome:
        ref = f"{transaction.account_id}/{transaction.source_transaction_id}"
        self.attempts[ref] = self.attempts.get(ref, 0) + 1
        failure = self._failure_for(transaction, self.attempts[ref])
        if failure is not None:
            raise failure
        return await super().post(transaction, idempotency_key=idempotency_key)


def fails_n_times(count: int, exception: Exception) -> Callable[..., Exception | None]:
    """Helper for the common FlakyDestination case: fail the first `count` attempts."""

    def rule(_: DestinationTransaction, attempt: int) -> Exception | None:
        return exception if attempt <= count else None

    return rule


class DryRunDestination:
    """Accepts everything, sends nothing, keeps the full list.

    Used by `--dry-run`. Paired with the runner's refusal to write the
    checkpoint in dry-run mode, so a rehearsal cannot cause the real run to
    skip records it never actually posted.

    Deliberately not a subclass of RecordingDestination. The two have the same shape
    today, but a change that makes the test fake more forgiving must not
    quietly make the production dry-run path more forgiving with it.
    """

    def __init__(self) -> None:
        self.would_post: list[tuple[DestinationTransaction, str]] = []
        self.closed = False

    async def post(
        self, transaction: DestinationTransaction, *, idempotency_key: str
    ) -> PostOutcome:
        self.would_post.append((transaction, idempotency_key))
        return PostOutcome(destination_id=None, deduplicated=False)

    def summary(self, limit: int = 10) -> Sequence[str]:
        lines = [
            f"{t.account_id}/{t.source_transaction_id}  "
            f"{t.business_date.isoformat()}  {t.amount} {t.currency}  key={key[:20]}..."
            for t, key in self.would_post[:limit]
        ]
        if len(self.would_post) > limit:
            lines.append(f"... and {len(self.would_post) - limit} more")
        return lines

    async def aclose(self) -> None:
        self.closed = True
