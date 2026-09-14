"""This API's own contract: request and response models, and the mappers that
translate to and from the upstream client's models.

Kept separate from the client package on purpose. The client describes someone
else's API and is regenerated; this describes ours and is not.

UpstreamContractError and `require` come from the client package rather than
living here, because the shared FastAPI exception handlers need to import the
type in order to catch it. See client_core.mapping.

Accounts and transfers map unconditionally: plain functions in mappers.py.
Transactions do not: see transaction_rules.py.
"""

from client_core import UpstreamContractError

from .mappers import (
    to_account_response,
    to_transaction_page,
    to_transfer_response,
    to_upstream_transfer_request,
)
from .models import (
    AccountResponse,
    TransactionPage,
    TransactionResponse,
    TransferCreateRequest,
    TransferResponse,
)
from .transaction_rules import map_transaction

__all__ = [
    "UpstreamContractError",
    "AccountResponse",
    "TransactionResponse",
    "TransactionPage",
    "TransferCreateRequest",
    "TransferResponse",
    "to_account_response",
    "to_transaction_page",
    "to_upstream_transfer_request",
    "to_transfer_response",
    "map_transaction",
]
