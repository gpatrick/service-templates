"""PLACEHOLDER MODELS for the Payments service. Replace with generated ones.

Same rules as models/accounts.py: generated from recorded samples via
`make models-payments`, never hand-edited, money as Decimal.

Kept in a separate module from Accounts deliberately. The two services
version independently, and a shared models file becomes a merge point for
changes that have nothing to do with each other.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class TransferRequest(BaseModel):
    """Outbound body for creating a transfer.

    Separate from Transfer because request and response shapes differ: the
    request has no id, no status, and no timestamps. Merging them would force
    those fields optional and lose the check that a caller supplied
    everything required.
    """

    model_config = ConfigDict(populate_by_name=True)

    from_account_id: str = Field(alias="fromAccountId")
    to_account_id: str = Field(alias="toAccountId")
    amount: Decimal
    currency: str
    description: str | None = None


class Transfer(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    transfer_id: str = Field(alias="transferId")
    from_account_id: str | None = Field(default=None, alias="fromAccountId")
    to_account_id: str | None = Field(default=None, alias="toAccountId")
    amount: Decimal | None = None
    currency: str | None = None
    status: str | None = None
    created_at: str | None = Field(default=None, alias="createdAt")
