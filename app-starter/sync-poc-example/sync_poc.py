#!/usr/bin/env python3
"""Source-to-destination sync. Reads every page, picks a posting type per
entry, builds the request, and posts it.

    python sync_poc.py            dry run: print what would be posted
    python sync_poc.py --post     actually post
    python sync_poc.py --limit 5  first 5 entries only

    pip install -e ../client-core
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from client_core import (
    APIError,
    APIStatusError,
    BaseClient,
    RetryPolicy,
    client_credentials_auth,
)
from pydantic import BaseModel, ConfigDict, Field

# YOUR models.
from models import LedgerEntry, PostingRequest

# ==========================================================================
#  1. CONFIG
# ==========================================================================

SOURCE_URL = os.environ["SRC_URL"]
SOURCE_TOKEN_URL = os.environ["SRC_TOKEN_URL"]
SOURCE_CLIENT_ID = os.environ["SRC_CLIENT_ID"]
SOURCE_CLIENT_SECRET = os.environ["SRC_CLIENT_SECRET"]

DEST_URL = os.environ["DST_URL"]
DEST_TOKEN_URL = os.environ["DST_TOKEN_URL"]
DEST_CLIENT_ID = os.environ["DST_CLIENT_ID"]
DEST_CLIENT_SECRET = os.environ["DST_CLIENT_SECRET"]

SOURCE_PATH = "/ledger/entries"
DEST_PATH = "/postings"

# Reads are idempotent and cheap to repeat, so they retry harder than writes.
SOURCE_RETRY = RetryPolicy(max_attempts=4, max_elapsed=20.0)

# POST is excluded from retries unless retry_non_idempotent is set. Do not set
# it: a lost response would become two postings.
DEST_RETRY = RetryPolicy(max_attempts=2, max_elapsed=30.0)


# ==========================================================================
#  2. SOURCE MODELS
#
#  LedgerEntry is yours, imported above. Only the page envelope lives here,
#  because it is about the endpoint rather than the record.
# ==========================================================================


class SourcePage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    entries: list[LedgerEntry] = Field(default_factory=list)
    next_cursor: str | None = Field(default=None, alias="nextCursor")


# ==========================================================================
#  3. DESTINATION MODELS
#
#  PostingRequest is yours, imported above. Only the response model lives
#  here.
# ==========================================================================


class DestinationResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    posting_id: str = Field(alias="postingId")


# ==========================================================================
#  4. SOURCE CLIENT
# ==========================================================================


class SourceClient(BaseClient):
    async def get_page(self, cursor: str | None) -> SourcePage:
        return await self._get(SOURCE_PATH, SourcePage, cursor=cursor)


# ==========================================================================
#  5. DESTINATION CLIENT
# ==========================================================================


class DestinationClient(BaseClient):
    async def create(self, payload: PostingRequest) -> DestinationResponse:
        return await self._post(DEST_PATH, DestinationResponse, body=payload)


# ==========================================================================
#  6. SELECTION
# ==========================================================================


def choose(entry: LedgerEntry) -> str | None:
    """Return a posting type, or None to skip the entry.

    Evaluated top to bottom, so specific cases first. A reversal has to be
    checked before entry_kind, because a reversal still carries the kind of
    the thing it reverses.
    """
    if entry.reverses_entry:
        return "REVERSAL"
    if entry.entry_kind == "charge":
        return "CHARGE"
    if entry.entry_kind == "spend" and entry.gross_amount < 0:
        return "CREDIT"
    if entry.entry_kind == "spend":
        return "DEBIT"
    return None


def build(posting_type: str, entry: LedgerEntry) -> PostingRequest:
    """Build the request for one entry.

    Every type sets the shared fields, then only the optional fields that type
    needs. Because BaseClient encodes with exclude_unset=True, the optionals
    you do not touch are left out of the JSON entirely rather than sent as
    null. With a single union-shaped model that is what you want: a CHARGE
    body carries chargeCode and nothing else extra.
    """
    common = {
        "posting_type": posting_type,
        "book_ref": entry.book,
        "source_ref": entry.entry_id,
        "effective_date": entry.value_date,
        "amount": entry.gross_amount,
        "currency": entry.iso_currency,
    }

    if posting_type == "DEBIT":
        return PostingRequest(
            **common,
            counterparty_name=entry.counterparty,
            note=entry.memo,
        )

    if posting_type == "CREDIT":
        # Magnitude, with direction carried by posting_type. Confirm this is
        # what the destination wants.
        return PostingRequest(
            **{**common, "amount": abs(entry.gross_amount)},
            note=entry.memo,
        )

    if posting_type == "REVERSAL":
        return PostingRequest(
            **{**common, "amount": abs(entry.gross_amount)},
            original_ref=entry.reverses_entry,
            note=entry.memo,
        )

    if posting_type == "CHARGE":
        if entry.charge_code is None:
            raise ValueError(f"{entry.entry_id}: CHARGE needs chargeCode, none present")
        return PostingRequest(**common, charge_code=entry.charge_code)

    raise ValueError(f"no builder for {posting_type}")


# ==========================================================================
#  7. RUNNER  --  unchanged
# ==========================================================================


def wire_body(payload: PostingRequest) -> dict:
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
        entries: list[LedgerEntry] = []
        cursor: str | None = None
        pages = 0
        while True:
            page = await source.get_page(cursor)
            pages += 1
            entries.extend(page.entries)
            if not page.next_cursor or page.next_cursor == cursor:
                break
            cursor = page.next_cursor

        print(f"read {len(entries)} entr(ies) from {pages} page(s)")
        if limit:
            entries = entries[:limit]

        counts: dict[str, int] = {}
        skipped: list[str] = []
        errors: list[str] = []
        posted = 0

        for index, entry in enumerate(entries, start=1):
            print(f"\n--- {index} {'-' * 56}")
            print("SOURCE   " + _block(entry.model_dump(mode="json", by_alias=True)))

            posting_type = choose(entry)
            if posting_type is None:
                skipped.append(entry.entry_id)
                print("TYPE     (no match, nothing to post)")
                continue

            try:
                payload = build(posting_type, entry)
            except ValueError as exc:
                errors.append(str(exc))
                print(f"TYPE     {posting_type}")
                print(f"ERROR    {exc}")
                continue

            counts[posting_type] = counts.get(posting_type, 0) + 1
            print(f"TYPE     {posting_type}")
            print(f"POST     {DEST_PATH}")
            print("BODY     " + _block(wire_body(payload)))

            if post:
                try:
                    result = await destination.create(payload)
                    print(f"         -> posted {result.posting_id}")
                    posted += 1
                except APIStatusError as exc:
                    errors.append(f"{entry.entry_id}: {exc}")
                    print(f"         -> REJECTED {exc}")
                except APIError as exc:
                    errors.append(f"{entry.entry_id}: {exc}")
                    print(f"         -> FAILED {exc}")

        print("\n" + "=" * 64)
        for posting_type in sorted(counts):
            print(f"  {counts[posting_type]:>4}  {posting_type}")
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
