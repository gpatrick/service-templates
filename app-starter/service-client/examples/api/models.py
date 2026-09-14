"""Response and request models for THIS API.

Deliberately separate from the upstream client's models. The client's models
are a faithful description of someone else's API and change when `make models`
regenerates them. These describe our contract with our own callers and change
only when we decide to change it.

Keeping them separate buys three things:

  1. A regenerated upstream field rename breaks a mapper at a known location
     instead of silently altering what our callers receive.
  2. We choose what to expose. Upstream internal status codes, cursors, and
     fields nobody outside needs can be dropped here.
  3. We can be stricter than the upstream. Its models are permissive because
     the API is inconsistent; ours can declare fields required and let the
     mapper decide what happens when they are missing.

FIELD NAMING: snake_case on the wire, matching the Python attributes. That is
a deliberate choice for a new API. If consumers expect camelCase, add
`model_config = ConfigDict(populate_by_name=True)` plus `alias=` on each field
and serialize with `by_alias=True`; do it before anyone integrates, since it
is a breaking change afterward.

MONEY: serialized as a JSON string, not a number. A JSON number invites
consumers to parse it as a float, which cannot represent most decimal amounts
exactly. A string pushes the decision back to them explicitly.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PlainSerializer

__all__ = [
    "TransactionKind",
    "AccountResponse",
    "TransactionResponse",
    "TransactionPage",
    "TransferCreateRequest",
    "TransferResponse",
]

# str(Decimal("25.00")) is "25.00": the scale the value carries is preserved,
# so an amount does not silently gain or lose trailing zeros in transit.
Money = Annotated[Decimal, PlainSerializer(str, return_type=str, when_used="json")]

# Every value `kind` can take. Adding a variant means adding it here first,
# which is the intended friction: a new response shape is a contract change.
TransactionKind = Literal["debit", "pending_debit", "credit", "fee", "reversal"]


class AccountResponse(BaseModel):
    account_id: str
    name: str | None = None
    status: str | None = None
    currency: str | None = None
    balance: Money | None = None
    available_balance: Money | None = None


class TransactionResponse(BaseModel):
    """One flat shape covering every transaction variant.

    The alternative is a union of per-variant response models, which is more
    precise but pushes narrowing onto every consumer and produces messy
    OpenAPI output. A flat shape with `kind` plus nullable variant-specific
    fields is kinder to callers at the cost of a looser schema. Decide this
    before anyone generates a client from the spec; it is breaking afterward.
    """

    transaction_id: str
    # Our own vocabulary, not the upstream's `type`. Decoupled on purpose: the
    # upstream can add or rename a type without changing our contract, and the
    # rules decide the mapping.
    #
    # A Literal because we control every value, unlike the upstream's `type`
    # which stays a plain str. This catches a typo in a builder at
    # construction rather than shipping it to a caller, and it publishes the
    # set as an enum in the OpenAPI schema so consumers can branch on it.
    kind: TransactionKind
    amount: Money | None = None
    currency: str | None = None
    description: str | None = None
    posted_at: str | None = None

    # Variant-specific. Populated only for the kinds they apply to.
    merchant: str | None = None
    fee_code: str | None = None
    reverses_transaction_id: str | None = None


class TransactionPage(BaseModel):
    """Pagination is exposed rather than hidden.

    The alternative is draining every page server-side and returning one list,
    which turns an unbounded upstream result into an unbounded response and an
    unbounded memory footprint. Handing the cursor to the caller keeps each
    request bounded.
    """

    transactions: list[TransactionResponse] = Field(default_factory=list)
    next_cursor: str | None = None


class TransferCreateRequest(BaseModel):
    """Inbound body for POST /transfers.

    Separate from the upstream's TransferRequest for the same reason the
    response models are separate: our callers should not have to track changes
    to someone else's request schema. The mapper translates.
    """

    from_account_id: str
    to_account_id: str
    # Accepts a JSON string or number and coerces to Decimal. Prefer sending a
    # string; a float literal in the request body has already lost precision
    # before it reaches us.
    amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    description: str | None = Field(default=None, max_length=140)


class TransferResponse(BaseModel):
    transfer_id: str
    from_account_id: str | None = None
    to_account_id: str | None = None
    amount: Money | None = None
    currency: str | None = None
    status: str | None = None
    created_at: datetime | None = None
