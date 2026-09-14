#!/usr/bin/env python3
"""Source-to-destination sync. Reads every page, picks a destination model
per transaction, builds it, and posts it.

    python sync_poc.py            dry run: print what would be posted
    python sync_poc.py --post     actually post
    python sync_poc.py --limit 5  first 5 transactions only

Everything you need to change is marked CHANGE ME. They run top to bottom:

    1. CONFIG               urls, credentials, paths
    2. SOURCE MODELS        your page and transaction models
    3. DESTINATION MODELS   one model per destination transaction type
    4. SOURCE CLIENT        the GET
    5. DESTINATION CLIENT   the POST
    6. SELECTION            which model, and how to build it

Section 7 is the runner; leave it alone.

    pip install -e ../client-core
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from decimal import Decimal

from client_core import (
    APIError,
    APIStatusError,
    BaseClient,
    RetryPolicy,
    client_credentials_auth,
)
from pydantic import BaseModel, ConfigDict, Field

# ==========================================================================
#  1. CONFIG  --  CHANGE ME
# ==========================================================================

SOURCE_URL = os.environ["SRC_URL"]
SOURCE_TOKEN_URL = os.environ["SRC_TOKEN_URL"]
SOURCE_CLIENT_ID = os.environ["SRC_CLIENT_ID"]
SOURCE_CLIENT_SECRET = os.environ["SRC_CLIENT_SECRET"]

DEST_URL = os.environ["DST_URL"]
DEST_TOKEN_URL = os.environ["DST_TOKEN_URL"]
DEST_CLIENT_ID = os.environ["DST_CLIENT_ID"]
DEST_CLIENT_SECRET = os.environ["DST_CLIENT_SECRET"]

SOURCE_PATH = "/transactions"
DEST_PATH = "/transactions"

# Reads are safe to repeat, so they retry harder than writes.
SOURCE_RETRY = RetryPolicy(max_attempts=4, max_elapsed=20.0)

# POST is excluded from retries unless retry_non_idempotent is set. Do not set
# it: a lost response would become two transactions.
DEST_RETRY = RetryPolicy(max_attempts=2, max_elapsed=30.0)


# ==========================================================================
#  2. SOURCE MODELS  --  CHANGE ME
#
#  Drop your models in here. Two are needed: the page envelope and the record.
# ==========================================================================


class SourceTransaction(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    transaction_id: str = Field(alias="transactionId")
    account_id: str = Field(alias="accountId")
    amount: Decimal
    currency: str
    type: str
    posted_at: str = Field(alias="postedAt")
    description: str | None = None
    merchant: str | None = None
    fee_code: str | None = Field(default=None, alias="feeCode")
    reversal_of: str | None = Field(default=None, alias="reversalOf")


class SourcePage(BaseModel):
    """The page envelope.

    For a bare JSON array instead:

        class SourcePage(RootModel[list[SourceTransaction]]):
            @property
            def transactions(self): return self.root
            @property
            def next_cursor(self): return None
    """

    model_config = ConfigDict(populate_by_name=True)

    transactions: list[SourceTransaction] = Field(default_factory=list)
    next_cursor: str | None = Field(default=None, alias="nextCursor")


# ==========================================================================
#  3. DESTINATION MODELS  --  CHANGE ME
#
#  One per destination transaction type. Drop yours in.
#
#  Keep `transaction_type` REQUIRED, not defaulted. BaseClient encodes bodies
#  with exclude_unset=True, so a field that is only a class default is never
#  sent. A defaulted discriminator would silently vanish from the wire.
# ==========================================================================


class DestinationBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    transaction_type: str = Field(alias="transactionType")
    account_id: str = Field(alias="accountId")
    external_reference: str = Field(alias="externalReference")
    business_date: date = Field(alias="businessDate")
    amount: Decimal
    currency: str


class PurchasePayload(DestinationBase):
    merchant_name: str | None = Field(default=None, alias="merchantName")
    description: str | None = None


class RefundPayload(DestinationBase):
    original_reference: str | None = Field(default=None, alias="originalReference")


class FeePayload(DestinationBase):
    fee_code: str = Field(alias="feeCode")


class DestinationResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    transaction_id: str = Field(alias="transactionId")


# ==========================================================================
#  4. SOURCE CLIENT  --  CHANGE ME
#
#  Subclass BaseClient, add one thin method. Auth, timeouts, retries, error
#  mapping and response validation come from the base class.
# ==========================================================================


class SourceClient(BaseClient):
    async def get_page(self, cursor: str | None) -> SourcePage:
        """_get sends **params as the query string. None params are dropped,
        so an unset cursor is simply not sent.

        Other paging styles:
            page number   page=cursor or 1
            offset        offset=cursor or 0, limit=PAGE_SIZE
        """
        return await self._get(SOURCE_PATH, SourcePage, cursor=cursor)


# ==========================================================================
#  5. DESTINATION CLIENT  --  CHANGE ME
# ==========================================================================


class DestinationClient(BaseClient):
    async def create(self, payload: DestinationBase) -> DestinationResponse:
        return await self._post(DEST_PATH, DestinationResponse, body=payload)


# ==========================================================================
#  6. SELECTION  --  CHANGE ME
#
#  choose() picks the type. build() constructs that type's model.
# ==========================================================================


def choose(txn: SourceTransaction) -> str | None:
    """Return a destination type name, or None to skip the record.

    Evaluated top to bottom, so specific cases first. A reversal has to be
    checked before `type == "purchase"`, because a reversal still arrives
    carrying the type of the thing it reverses.
    """
    if txn.reversal_of:
        return "REFUND"
    if txn.type == "fee":
        return "FEE"
    if txn.type == "purchase" and txn.amount < 0:
        return "REFUND"
    if txn.type == "purchase":
        return "PURCHASE"
    return None


def build(kind: str, txn: SourceTransaction) -> DestinationBase:
    """Build the payload for one transaction."""
    common = {
        "transaction_type": kind,
        "account_id": txn.account_id,
        "external_reference": txn.transaction_id,
        # CHANGE ME if the source exposes a real business date. This is the
        # date part of a timestamp, which is a different thing: cut-off times
        # and timezones move the business date.
        "business_date": date.fromisoformat(txn.posted_at[:10]),
        "amount": txn.amount,
        "currency": txn.currency,
    }

    if kind == "PURCHASE":
        return PurchasePayload(
            **common,
            merchant_name=txn.merchant,
            description=txn.description,
        )

    if kind == "REFUND":
        # CHANGE ME if the destination wants a signed amount here. Many take
        # the direction from the type and expect a magnitude; guessing wrong
        # posts cleanly and reconciles wrongly.
        return RefundPayload(
            **{**common, "amount": abs(txn.amount)},
            original_reference=txn.reversal_of,
        )

    if kind == "FEE":
        if txn.fee_code is None:
            raise ValueError(f"{txn.transaction_id}: FEE needs feeCode, none present")
        return FeePayload(**common, fee_code=txn.fee_code)

    raise ValueError(f"no builder for {kind}")


# ==========================================================================
#  7. RUNNER  --  leave alone
# ==========================================================================


def wire_body(payload: DestinationBase) -> dict:
    """Exactly what BaseClient will send. Mirrors transport._encode."""
    return payload.model_dump(mode="json", by_alias=True, exclude_unset=True)


async def run(post: bool, limit: int | None) -> None:
    source = SourceClient(
        SOURCE_URL,
        auth=client_credentials_auth(
            SOURCE_TOKEN_URL,
            client_id=SOURCE_CLIENT_ID,
            client_secret=SOURCE_CLIENT_SECRET,
        ),
        retry=SOURCE_RETRY,
        timeout=15.0,
    )
    destination = DestinationClient(
        DEST_URL,
        auth=client_credentials_auth(
            DEST_TOKEN_URL,
            client_id=DEST_CLIENT_ID,
            client_secret=DEST_CLIENT_SECRET,
        ),
        retry=DEST_RETRY,
        timeout=30.0,
    )

    try:
        transactions: list[SourceTransaction] = []
        cursor: str | None = None
        pages = 0
        while True:
            page = await source.get_page(cursor)
            pages += 1
            transactions.extend(page.transactions)
            if not page.next_cursor or page.next_cursor == cursor:
                break
            cursor = page.next_cursor

        print(f"read {len(transactions)} transaction(s) from {pages} page(s)")
        if limit:
            transactions = transactions[:limit]

        counts: dict[str, int] = {}
        skipped: list[str] = []
        errors: list[str] = []
        posted = 0

        for index, txn in enumerate(transactions, start=1):
            print(f"\n--- {index} {'-' * 56}")
            print("SOURCE   " + _block(txn.model_dump(mode="json", by_alias=True)))

            kind = choose(txn)
            if kind is None:
                skipped.append(txn.transaction_id)
                print("TYPE     (no match, nothing to post)")
                continue

            try:
                payload = build(kind, txn)
            except ValueError as exc:
                errors.append(str(exc))
                print(f"TYPE     {kind}")
                print(f"ERROR    {exc}")
                continue

            counts[kind] = counts.get(kind, 0) + 1
            print(f"TYPE     {kind}")
            print(f"POST     {DEST_PATH}")
            print("BODY     " + _block(wire_body(payload)))

            if post:
                try:
                    result = await destination.create(payload)
                    print(f"         -> posted {result.transaction_id}")
                    posted += 1
                except APIStatusError as exc:
                    # 4xx. The request is wrong and will be wrong the same way
                    # next time: usually the model or the mapping.
                    errors.append(f"{txn.transaction_id}: {exc}")
                    print(f"         -> REJECTED {exc}")
                except APIError as exc:
                    # Connection, timeout, 5xx, rate limit.
                    errors.append(f"{txn.transaction_id}: {exc}")
                    print(f"         -> FAILED {exc}")

        print("\n" + "=" * 64)
        for kind in sorted(counts):
            print(f"  {counts[kind]:>4}  {kind}")
        if skipped:
            print(f"  {len(skipped):>4}  no match: {', '.join(skipped)}")
        if errors:
            print(f"\n  {len(errors)} error(s):")
            for line in errors:
                print(f"    {line}")
        print(f"\n  {posted} posted")
        if not post:
            print("  dry run. re-run with --post when the bodies look right.")
    finally:
        await source.aclose()
        await destination.aclose()


def _block(data: dict) -> str:
    return json.dumps(data, indent=2, default=str).replace("\n", "\n         ")


def main() -> None:
    parser = argparse.ArgumentParser(description="Source to destination sync.")
    parser.add_argument("--post", action="store_true",
                        help="actually post (default is a dry run)")
    parser.add_argument("--limit", type=int, help="only process the first N")
    args = parser.parse_args()
    asyncio.run(run(args.post, args.limit))


if __name__ == "__main__":
    sys.exit(main())
