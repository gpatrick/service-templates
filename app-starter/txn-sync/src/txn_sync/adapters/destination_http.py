"""The destination system. CUSTOMIZE THIS FILE.

This is where transactions are posted. It contains the only HTTP client in the
project that the service-client package does not already provide, because that
package models Accounts and Payments and the destination here is neither: it
takes transactions directly.

WHY THE CLIENT LIVES HERE RATHER THAN IN service-client

Adding a TransactionsClient to the library would mean changing a package that
a working FastAPI service already depends on, to serve one consumer. BaseClient
is explicitly designed to be subclassed for exactly this, and subclassing it
here gets all of the library's retry handling, error mapping and response
validation without touching it.

If a third consumer ever needs the same endpoints, promote this class into
service-client as `transactions.py`. It is a file move; nothing in this module
depends on living here.

WHAT TO CONFIRM BEFORE THE FIRST REAL RUN

  1. TRANSACTIONS_PATH. Currently "/transactions", which is a guess.
  2. IDEMPOTENCY_HEADER. The single most consequential constant in the
     project; see its comment.
  3. The request and response models below. They are placeholders in exactly
     the sense service-client's models are, and the same regeneration rules
     apply: generate from recorded responses, money as Decimal, never hand
     edit.
  4. Whether a duplicate post is rejected, deduplicated, or silently accepted
     twice. That answer decides whether
     SYNC_RETRY_WRITES_ON_TRANSIENT can ever be turned on.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field
from service_client import BaseClient, RetryPolicy

from ..records import DestinationTransaction, PostOutcome

__all__ = [
    "TransactionsClient",
    "HttpTransactionDestination",
    "IDEMPOTENCY_HEADER",
    "TRANSACTIONS_PATH",
    "DESTINATION_RETRY",
]

# CONFIRM THIS PATH against the real API.
TRANSACTIONS_PATH = "/transactions"

# CONFIRM THIS HEADER NAME. It carries more weight than anything else here.
#
# "Idempotency-Key" is the common convention, used by Stripe and by the IETF
# draft, but some APIs expect "X-Idempotency-Key", some expect a body field,
# and some have no such concept at all.
#
# The failure mode if this is wrong is the quiet one: the destination ignores
# an unrecognized header, returns 200, and every retry creates a duplicate
# transaction. Nothing errors. Nobody finds out until a reconciliation.
#
# The way to confirm it is to post the same body twice with the same key
# against a non-production environment and check whether the second call
# creates a second record. Do that before turning on
# SYNC_RETRY_WRITES_ON_TRANSIENT, and ideally before the first production run.
IDEMPOTENCY_HEADER = "Idempotency-Key"

# One attempt for the write itself, no automatic repeat. max_attempts=2 covers
# the connection-level case only; POST is excluded from retries by
# RetryPolicy.should_retry unless retry_non_idempotent is set, and setting it
# on this client would be a serious bug. Retry policy for writes lives in the
# runner, where it is a conscious, configured decision.
DESTINATION_RETRY = RetryPolicy(max_attempts=2, max_elapsed=30.0)


class TransactionRequest(BaseModel):
    """PLACEHOLDER. Outbound body for creating a transaction.

    Separate from TransactionResponse because request and response shapes
    differ: the request has no id, no status and no timestamps. Merging them
    forces those optional and loses the check that the caller supplied
    everything required.

    Aliases are camelCase on the assumption the destination is a typical JSON
    API. Change them to whatever it actually wants; `populate_by_name` means
    the Python-side names do not have to change with them.
    """

    model_config = ConfigDict(populate_by_name=True)

    account_id: str = Field(alias="accountId")
    external_reference: str = Field(alias="externalReference")
    business_date: date = Field(alias="businessDate")
    amount: Decimal
    currency: str
    description: str | None = None


class TransactionResponse(BaseModel):
    """PLACEHOLDER. What the destination returns after a create."""

    model_config = ConfigDict(populate_by_name=True)

    transaction_id: str = Field(alias="transactionId")
    status: str | None = None
    created_at: str | None = Field(default=None, alias="createdAt")

    # If the destination tells you a request was deduplicated, map it here.
    # Many do not, in which case leave this None; see PostOutcome, which
    # treats "we do not know" as False rather than guessing from the status
    # code. A 200-versus-201 distinction is a plausible signal but only if
    # their documentation actually promises it.
    duplicate: bool | None = None


class TransactionsClient(BaseClient):
    """Typed client for the destination's transactions endpoint.

    Follows the same rules as the clients in service-client: one thin method
    per operation, required parameters stay required, return a model rather
    than a dict. All retry, error mapping and validation behavior comes from
    BaseClient, so every endpoint added here gets it without remembering to.
    """

    async def create_transaction(
        self, transaction: TransactionRequest, *, idempotency_key: str
    ) -> TransactionResponse:
        """Create one transaction.

        NOT retried automatically, and that default must stay. A retried POST
        after a timeout can create the record twice, because the request may
        have succeeded with only the response lost.

        `idempotency_key` is required rather than optional. An optional key is
        a key someone forgets, and forgetting it produces no error and no
        visible symptom.
        """
        # Annotated explicitly because BaseClient is untyped to mypy (see the
        # override in pyproject.toml), so _post returns Any and the return
        # type would silently degrade to Any for every caller.
        response: TransactionResponse = await self._post(
            TRANSACTIONS_PATH,
            TransactionResponse,
            body=transaction,
            headers={IDEMPOTENCY_HEADER: idempotency_key},
        )
        return response


class HttpTransactionDestination:
    """Adapts TransactionsClient to the TransactionDestination port.

    The adapter is thin on purpose. Anything that looks like a decision, such
    as whether to retry or how to classify a failure, belongs in the runner
    and errors.py where it is tested and where there is one copy of it.
    """

    def __init__(self, client: TransactionsClient, *, source_system: str) -> None:
        self._client = client
        self._source_system = source_system

    async def post(
        self, transaction: DestinationTransaction, *, idempotency_key: str
    ) -> PostOutcome:
        body = TransactionRequest(
            account_id=transaction.account_id,
            external_reference=transaction.source_transaction_id,
            business_date=transaction.business_date,
            amount=transaction.amount,
            currency=transaction.currency,
            description=transaction.description,
        )
        response = await self._client.create_transaction(
            body, idempotency_key=idempotency_key
        )
        return PostOutcome(
            destination_id=response.transaction_id,
            deduplicated=bool(response.duplicate),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def make_http_destination(
    *,
    base_url: str,
    auth: httpx.Auth,
    source_system: str,
    timeout: float = 30.0,
    extra: dict[str, Any] | None = None,
) -> HttpTransactionDestination:
    """Construct the real destination. Kept here so wiring.py stays declarative.

    The timeout is longer than the source's. A write into another system's
    ledger is often slower than a read, and a timeout that fires while the
    destination is still committing produces precisely the lost-response case
    the idempotency key exists to handle. Better to wait.
    """
    client = TransactionsClient(
        base_url,
        auth=auth,
        timeout=timeout,
        retry=DESTINATION_RETRY,
        user_agent=f"txn-sync/0.1 ({source_system})",
        **(extra or {}),
    )
    return HttpTransactionDestination(client, source_system=source_system)
