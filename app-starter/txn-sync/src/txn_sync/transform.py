"""Source record to destination record. CUSTOMIZE THIS FILE.

This is one of the three places that change when the real systems arrive; the
other two are the adapters. It is a pure function over dataclasses, so the
field mapping can be reviewed by someone who knows the two systems without
them having to read any async code, and every case can be unit tested without
a network.

WHAT TO DO HERE ON THE CLIENT'S MACHINE

  1. Fill in `FIELD_NOTES` with the real correspondence, one line per
     destination field, including the ones that are constants and the ones
     that have no source analogue. That table is the artifact the client's
     team will actually review. Keeping it next to the code rather than in a
     separate document is the only way it stays true.

  2. Decide the sign convention. This is the single most common way an
     integration like this goes wrong quietly. If the source signs debits
     negative and the destination expects magnitude plus a direction flag,
     a straight copy produces records that reconcile to zero and look fine
     until someone sums them. `_direction` below is a stub for exactly this;
     confirm it before the first real run.

  3. Decide what `extra` must carry: ledger codes, posting channels, batch
     references. Put them here rather than in the destination adapter so the values
     are visible in one place.

WHAT NOT TO DO HERE

  No I/O, no clock reads, no randomness. A transform that consults the current
  time cannot be tested and cannot be replayed, and replay is how a
  dead-lettered record gets fixed and resubmitted.

  No dropping of records. If a record should not cross over, that is a rule,
  and it belongs in selection.py where the skip gets counted and named. A
  transform that returns None for unwanted records hides them from the
  summary.
"""

from __future__ import annotations

from decimal import Decimal

from .records import DestinationTransaction, SourceTransaction

__all__ = ["transform", "FIELD_NOTES", "TransformError"]


class TransformError(ValueError):
    """The record cannot be represented in the destination's shape.

    Terminal by classification: the same record produces the same failure on
    every run, so it belongs in the dead-letter file rather than in a retry
    loop. Raise this rather than letting a KeyError or AttributeError escape,
    because the message is what someone reads in the dead-letter file and
    "KeyError: 'ledgerCode'" does not say which record or why it mattered.
    """


# One line per destination field. Fill in against the real spec.
#
#   destination field          <- source
#   -------------------------     ------------------------------------------
#   accountId                  <- SourceTransaction.account_id
#   externalReference          <- SourceTransaction.transaction_id
#   businessDate               <- SourceTransaction.business_date
#   amount                     <- SourceTransaction.amount (SIGN: confirm)
#   currency                   <- SourceTransaction.currency
#   description                <- SourceTransaction.description
#   ...                        <- CONSTANT / UNMAPPED: decide and record here
FIELD_NOTES: dict[str, str] = {
    "account_id": "source account_id, verbatim",
    "source_transaction_id": "source transaction_id, carried for reconciliation",
    "business_date": "source business_date",
    "amount": "source amount; SIGN CONVENTION UNCONFIRMED",
    "currency": "source currency, uppercased",
    "description": "source description, truncated to DESCRIPTION_MAX_LEN",
}

# Destinations commonly cap free-text fields and reject anything longer with a
# 400 that names the field but not the length. Truncating here, where it is
# visible and testable, beats discovering the limit one dead letter at a time.
# CONFIRM THE REAL LIMIT; this is a guess.
DESCRIPTION_MAX_LEN = 140


def transform(source: SourceTransaction) -> DestinationTransaction:
    """Map one source transaction onto the destination's shape.

    Deliberately total: every SourceTransaction either produces a
    DestinationTransaction or raises TransformError. There is no third
    outcome, because a transform that can silently return nothing is a place
    for records to disappear.
    """
    if not source.currency:
        raise TransformError(
            f"{source.account_id}/{source.transaction_id}: currency is missing, "
            f"and the destination cannot infer it"
        )

    amount = _signed_amount(source)

    description = source.description
    if description is not None and len(description) > DESCRIPTION_MAX_LEN:
        description = description[:DESCRIPTION_MAX_LEN]

    return DestinationTransaction(
        account_id=source.account_id,
        source_transaction_id=source.transaction_id,
        business_date=source.business_date,
        amount=amount,
        currency=source.currency.upper(),
        description=description,
        extra=_extra_fields(source),
    )


def _signed_amount(source: SourceTransaction) -> Decimal:
    """Return the amount in the destination's sign convention.

    CONFIRM THIS BEFORE THE FIRST REAL RUN. Right now it is a straight copy,
    which is correct only if both systems sign debits and credits the same
    way. The three conventions in common use are:

      * signed amount, debits negative
      * signed amount, debits positive
      * unsigned magnitude plus a separate direction or type field

    Crossing between any two of them without noticing produces records that
    post successfully and reconcile wrongly, which is the worst combination
    available: no error anywhere, and a discrepancy that surfaces weeks later
    in someone else's report.

    If the destination wants magnitude plus direction, return abs() here and
    put the direction into _extra_fields.
    """
    return source.amount


def _extra_fields(source: SourceTransaction) -> dict[str, object]:
    """Destination fields with no direct source analogue. FILL THIS IN.

    Empty by default so the first integration test posts the minimum viable
    body and the destination tells you what else it requires. That is a
    faster way to find the mandatory fields than reading a spec that may be
    out of date, provided you are pointed at a non-production environment.

    Values that come from `source.raw` belong here. Reach into raw with .get
    and raise TransformError on a missing value that is genuinely required,
    rather than letting a KeyError escape as an unclassified failure.
    """
    return {}
