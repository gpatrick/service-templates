"""Stand-in for YOUR models module. You already have this; it is here so the
example below is runnable.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class LedgerEntry(BaseModel):
    """Your SOURCE model."""

    model_config = ConfigDict(populate_by_name=True)

    entry_id: str = Field(alias="entryId")
    book: str
    gross_amount: Decimal = Field(alias="grossAmount")
    iso_currency: str = Field(alias="isoCurrency")
    entry_kind: str = Field(alias="entryKind")
    value_date: date = Field(alias="valueDate")
    counterparty: str | None = None
    charge_code: str | None = Field(default=None, alias="chargeCode")
    reverses_entry: str | None = Field(default=None, alias="reversesEntry")
    memo: str | None = None


class PostingRequest(BaseModel):
    """Your DESTINATION model. One model, many types.

    The optional fields are the union of what every posting type needs. Which
    ones you set depends on posting_type.
    """

    model_config = ConfigDict(populate_by_name=True)

    posting_type: str = Field(alias="postingType")
    book_ref: str = Field(alias="bookRef")
    source_ref: str = Field(alias="sourceRef")
    effective_date: date = Field(alias="effectiveDate")
    amount: Decimal
    currency: str

    counterparty_name: str | None = Field(default=None, alias="counterpartyName")
    charge_code: str | None = Field(default=None, alias="chargeCode")
    original_ref: str | None = Field(default=None, alias="originalRef")
    note: str | None = None
