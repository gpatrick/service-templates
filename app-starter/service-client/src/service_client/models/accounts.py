"""PLACEHOLDER MODELS for the Accounts service. Replace with generated ones.

These are a guess at the resource shapes so the client and tests run end to
end. Every field needs confirming against recorded responses.

Regenerate with `make models-accounts` once samples exist under
samples/accounts/. That overwrites this whole file, which is intentional:
models are a build artifact, not source. Corrections belong in the sample
corpus or in the intermediate JSON Schema, never here. A hand edit is lost on
the next regeneration and the loss is silent until a record that exercises it
reaches production.

Two things to get right when you replace this:

  * Nullability must reflect what the API actually returns across the whole
    sample set, not what its published spec claims.
  * Money is Decimal, never float. If the API sends amounts as JSON numbers,
    check whether they are minor units (an integer count of cents) before
    accepting what the generator infers: datamodel-codegen reads a JSON
    number as float, and binary floating point cannot represent most decimal
    amounts exactly. The error compounds across a summed transaction list.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, RootModel


class Account(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    account_id: str = Field(alias="accountId")
    # Guesses below. Confirm which the API returns and which are optional.
    # Anything absent from every sample comes out as Any and silently
    # accepts anything.
    name: str | None = None
    status: str | None = None
    currency: str | None = None
    balance: Decimal | None = None
    available_balance: Decimal | None = Field(default=None, alias="availableBalance")


class Transaction(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    transaction_id: str = Field(alias="transactionId")
    account_id: str | None = Field(default=None, alias="accountId")
    amount: Decimal | None = None
    currency: str | None = None
    description: str | None = None
    posted_at: str | None = Field(default=None, alias="postedAt")

    # Discriminator. Left as a plain str rather than a Literal union because
    # the upstream may add values we have not seen, and a Literal would turn
    # an unknown type into a parse failure for the whole page. The mapper
    # raises on an unknown value instead, which fails one row's worth of
    # request rather than every request containing one.
    type: str | None = None

    # Variant-specific fields. Present or absent depending on `type`; the
    # rules in the application layer decide what each combination means.
    merchant: str | None = None
    fee_code: str | None = Field(default=None, alias="feeCode")
    reversal_of: str | None = Field(default=None, alias="reversalOf")


class TransactionList(BaseModel):
    """Envelope for a paginated transaction page.

    If the endpoint returns a bare JSON array instead, change this to
    `RootModel[list[Transaction]]` and adjust AccountsClient.iter_transactions,
    which reads `.transactions` and `.next_cursor`.
    """

    model_config = ConfigDict(populate_by_name=True)

    transactions: list[Transaction] = Field(default_factory=list)
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class AccountList(RootModel[list[Account]]):
    """Only needed if a list-accounts endpoint exists. Delete if not."""

    @property
    def accounts(self) -> list[Account]:
        return self.root
