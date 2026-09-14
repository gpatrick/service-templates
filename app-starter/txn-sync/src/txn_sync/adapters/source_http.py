"""The source system. CUSTOMIZE THIS FILE.

Reads transactions out of the source and converts them into the neutral
SourceTransaction that the rest of the job works with. It is built on
service-client's AccountsClient, which already models
`GET /accounts/{id}/transactions` and already follows pagination.

WHAT TO CONFIRM BEFORE THE FIRST REAL RUN

  1. The query parameter names for the date window. AccountsClient sends
     `startDate` and `endDate`; if the real API calls them something else, an
     unrecognized parameter is usually IGNORED rather than rejected, so the
     call succeeds and returns the account's entire history. The job survives
     this because selection.py re-checks the window and the extra records come out
     as OUTSIDE_WINDOW skips, but the read is enormously more expensive than
     it should be. A run whose summary is mostly OUTSIDE_WINDOW skips is
     telling you these parameter names are wrong.

  2. The date format. ISO `YYYY-MM-DD` is assumed. Some APIs want
     `MM/DD/YYYY`, and a wrongly formatted date is another quietly ignored
     parameter.

  3. `_business_date`. The source model carries `posted_at` as a string, and
     which timestamp field represents the BUSINESS date is a real question
     rather than a formality. Posting timestamps near midnight land on the
     wrong business day under any timezone mismatch, and the business date is
     part of every idempotency key, so getting it wrong changes keys and
     defeats deduplication. If the source exposes an explicit business or
     value date, use that field and delete the fallback.

  4. Whether amounts are minor units. If the API sends 1234 meaning 12.34,
     divide here, using Decimal, never float.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx
from service_client import ACCOUNTS_RETRY, AccountsClient
from service_client.models.accounts import Transaction as UpstreamTransaction

from ..records import DateWindow, SourceTransaction
from ..transform import TransformError

__all__ = ["HttpTransactionSource", "make_http_source"]

# Set True if the source sends amounts as an integer count of minor units.
# CONFIRM THIS. A factor-of-100 error posts successfully and reconciles
# wrongly, which is the hardest kind of mistake to notice.
AMOUNTS_ARE_MINOR_UNITS = False


class HttpTransactionSource:
    """Adapts AccountsClient to the TransactionSource port."""

    def __init__(self, client: AccountsClient, *, page_size: int | None = None) -> None:
        self._client = client
        self._page_size = page_size

    async def fetch(
        self, account_id: str, window: DateWindow
    ) -> AsyncIterator[SourceTransaction]:
        # The window is pushed down to the API rather than filtered here, so
        # the source does the work instead of the network. selection.py re-checks
        # it regardless; see point 1 in the module docstring for why that
        # belt-and-braces check is not paranoia.
        #
        # end is exclusive in DateWindow. Whether the API's endDate is
        # inclusive is a question for their documentation: if it is, sending
        # window.end pulls in one extra day, which surfaces as OUTSIDE_WINDOW
        # skips rather than as bad data.
        async for upstream in self._client.iter_transactions(
            account_id,
            start_date=window.start.isoformat(),
            end_date=window.end.isoformat(),
            page_size=self._page_size,
        ):
            yield _to_source_transaction(upstream, account_id)

    async def aclose(self) -> None:
        await self._client.aclose()


def _to_source_transaction(
    upstream: UpstreamTransaction, account_id: str
) -> SourceTransaction:
    """Convert one upstream model into the neutral record.

    Raises TransformError rather than letting a TypeError or AttributeError
    escape. Both are classified as terminal, but only one of them produces a
    dead-letter line that says which record and which field.
    """
    transaction_id = upstream.transaction_id
    if not transaction_id:
        raise TransformError(
            f"account {account_id}: a transaction arrived without an id, so it "
            f"cannot be given a stable idempotency key"
        )

    # The upstream model marks account_id optional. Prefer what it sent, fall
    # back to the account we asked about. They should agree; if they ever do
    # not, that is worth knowing, because everything downstream is scoped by
    # account.
    resolved_account = upstream.account_id or account_id
    if upstream.account_id and upstream.account_id != account_id:
        raise TransformError(
            f"transaction {transaction_id} was returned under account "
            f"{account_id} but claims account {upstream.account_id}; "
            f"idempotency keys and checkpoints are both scoped by account, so "
            f"this must be resolved rather than guessed at"
        )

    if upstream.amount is None:
        raise TransformError(f"{resolved_account}/{transaction_id}: amount is missing")
    if not upstream.currency:
        raise TransformError(f"{resolved_account}/{transaction_id}: currency is missing")

    amount = _amount(upstream.amount, resolved_account, transaction_id)
    posted_at = _parse_timestamp(upstream.posted_at)

    return SourceTransaction(
        account_id=resolved_account,
        transaction_id=transaction_id,
        business_date=_business_date(posted_at, resolved_account, transaction_id),
        amount=amount,
        currency=upstream.currency,
        posted_at=posted_at,
        kind=upstream.type,
        description=upstream.description,
        raw=upstream.model_dump(mode="json", by_alias=True),
    )


def _amount(value: Decimal, account_id: str, transaction_id: str) -> Decimal:
    if not AMOUNTS_ARE_MINOR_UNITS:
        return value
    try:
        return Decimal(value) / Decimal(100)
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise TransformError(
            f"{account_id}/{transaction_id}: amount {value!r} is not convertible "
            f"from minor units"
        ) from exc


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    # fromisoformat gained Z support in 3.11; this project targets 3.10.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        # Not fatal on its own: _business_date decides whether the job can
        # proceed without it. Returning None keeps the decision in one place.
        return None


def _business_date(
    posted_at: datetime | None, account_id: str, transaction_id: str
) -> date:
    """Derive the business date. CONFIRM THIS AGAINST THE SOURCE.

    Currently the UTC date of `posted_at`. That is a defensible default and it
    is also the single assumption in this file most likely to be wrong,
    because business date is a ledger concept rather than a clock reading:
    cut-off times, local timezones and weekend rules all move it.

    It matters beyond correctness of the field itself. The business date is
    part of every idempotency key, so changing this later changes every key
    and records already posted can post again.
    """
    if posted_at is None:
        raise TransformError(
            f"{account_id}/{transaction_id}: no usable timestamp, so the business "
            f"date cannot be derived, and it is part of the idempotency key"
        )
    if posted_at.tzinfo is None:
        # A naive timestamp is ambiguous. Assuming UTC is a choice; it is made
        # explicitly here rather than left to whatever the local machine's
        # timezone happens to be, because a scheduler running in one region and
        # a developer testing in another would otherwise derive different
        # business dates, and therefore different keys, from the same record.
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    return posted_at.astimezone(timezone.utc).date()


def make_http_source(
    *,
    base_url: str,
    auth: httpx.Auth,
    timeout: float = 15.0,
    page_size: int | None = None,
) -> HttpTransactionSource:
    """Construct the real source.

    Uses ACCOUNTS_RETRY from the library: reads are idempotent and cheap to
    repeat, so they retry harder than anything on the write side. Nothing is
    waiting on this job, so the retry budget can be generous compared with the
    web service's, which has a gateway timeout to race.
    """
    client = AccountsClient(base_url, auth=auth, timeout=timeout, retry=ACCOUNTS_RETRY)
    return HttpTransactionSource(client, page_size=page_size)
