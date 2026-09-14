"""Translation between upstream client models and this API's models.

Pure functions: an upstream model in, one of ours out. No I/O, no client, no
request context. That makes them trivial to test and makes the tests the place
where an upstream change surfaces.

Standalone functions rather than classmethods on the response models, because
a response may eventually combine more than one upstream call (a transfer plus
the account names it references, say) and a classmethod on one target implies
a single source.

WHEN A REQUIRED FIELD IS MISSING: use `_require`. It raises
UpstreamContractError naming the upstream field, which the app maps to 502.

Note that a field the CLIENT model also declares required never reaches
`_require`: the client raises ResponseValidationError while parsing, and that
already maps to 502. `_require` matters for fields the client model treats as
optional, and as a guard for the case where a future `make models` run turns a
required field optional because the sample corpus happened to miss it. That
regeneration would otherwise quietly start returning null to our callers.

Three options exist for any given field and the right one differs per field:

  * Declare it optional in our model too, and pass it through.
  * Substitute a default here, with a comment saying why that default is safe.
  * Treat it as a contract violation via `_require`.

Whichever you pick, it is decided once and visible here rather than being an
implicit None that propagates into a response.
"""

from __future__ import annotations

from client_core import maps_upstream, require

from service_client.models.accounts import Account, TransactionList
from service_client.models.payments import Transfer, TransferRequest

from .models import (
    AccountResponse,
    TransactionPage,
    TransferCreateRequest,
    TransferResponse,
)
from .transaction_rules import map_transaction

__all__ = [
    "to_account_response",
    "to_transaction_page",
    "to_upstream_transfer_request",
    "to_transfer_response",
]


# -- accounts --------------------------------------------------------------


@maps_upstream
def to_account_response(src: Account) -> AccountResponse:
    return AccountResponse(
        # Required by our contract. Today the client model requires it too, so
        # a missing id fails earlier as ResponseValidationError; this stays as
        # the guard for a regeneration that makes the field optional.
        account_id=require(src.account_id, "accountId", resource="Account"),
        # Everything below stays optional. The upstream is inconsistent about
        # these and a missing display name is not worth failing a request for.
        name=src.name,
        status=src.status,
        currency=src.currency,
        balance=src.balance,
        available_balance=src.available_balance,
    )


# -- transactions ----------------------------------------------------------


@maps_upstream
def to_transaction_page(src: TransactionList) -> TransactionPage:
    """Rows go through transaction_rules.map_transaction, not a plain mapper.

    Transactions are the one resource here whose shape depends on the payload.
    See transaction_rules.py for why that dispatch is a rule table rather than
    an if-chain.
    """
    # accountId is dropped: the caller supplied it in the path, so echoing it
    # on every row is noise.
    return TransactionPage(
        transactions=[map_transaction(t) for t in src.transactions],
        next_cursor=src.next_cursor,
    )


# -- transfers -------------------------------------------------------------


@maps_upstream
def to_upstream_transfer_request(src: TransferCreateRequest) -> TransferRequest:
    """Inbound direction: our request model to the upstream's.

    Nothing is required-checked here. Our model already validated it, which is
    the point of having a separate inbound shape: a malformed body is rejected
    with 422 before any upstream call is attempted.
    """
    return TransferRequest(
        from_account_id=src.from_account_id,
        to_account_id=src.to_account_id,
        amount=src.amount,
        currency=src.currency,
        description=src.description,
    )


@maps_upstream
def to_transfer_response(src: Transfer) -> TransferResponse:
    return TransferResponse(
        # Required by our contract: without an id the caller cannot reconcile
        # or follow up on a transfer, which matters more here than on a read.
        transfer_id=require(src.transfer_id, "transferId", resource="Transfer"),
        from_account_id=src.from_account_id,
        to_account_id=src.to_account_id,
        amount=src.amount,
        currency=src.currency,
        status=src.status,
        created_at=src.created_at,
    )
