"""Conditional mapping for transactions. THIS FILE IS THE EXCEPTION.

Accounts and transfers map unconditionally, so their mappers are plain
functions in mappers.py. Transactions do not: the shape of the response
depends on the upstream `type` and, within a type, on which fields are
populated. That needs dispatch, and dispatch is what this file is.

TWO STAGES, kept separate on purpose:

  1. The tag (`type`) picks a family, via a dict lookup.
  2. Within a family, an ordered rule list refines.

Flattening these into one predicate list keyed on tag-plus-conditions is the
version that becomes unmaintainable: every rule then re-checks the tag, and
the families stop being visible.

FIRST MATCH WINS. Rules are tried in list order and the first match returns.
That is the right model here because the variants are mutually exclusive
outcomes, not effects that combine. If you ever need two conditions to BOTH
affect one response, do not add a flag to a rule: switch to a base build plus
applied modifiers. Retrofitting first-match rules to accumulate is a rewrite
of every rule, because each one assumes it owns the whole output.

NEVER DEFAULT SILENTLY. An unmatched payload raises. A catch-all that returns
a half-populated response is the bug found three weeks later when someone asks
why a category of transactions shows a zero amount.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from client_core import UpstreamContractError, require

from service_client.models.accounts import Transaction

from .models import TransactionKind, TransactionResponse

__all__ = [
    "map_transaction",
    "Rule",
    "FAMILIES",
    "DEBIT_RULES",
    "CREDIT_RULES",
    "FEE_RULES",
]


@dataclass(frozen=True)
class Rule:
    """One condition and the builder that runs when it matches.

    A dataclass rather than a bare tuple so the name appears in errors and
    logs: knowing which rule fired without attaching a debugger is most of the
    value of writing rules down.
    """

    name: str
    matches: Callable[[Transaction], bool]
    build: Callable[[Transaction], TransactionResponse]


# -- builders --------------------------------------------------------------
# Each returns a complete response. They do not call each other; a builder
# that delegates to another is a sign the variants are not actually exclusive
# and the modifier shape is the right one instead.


def _base(
    src: Transaction, *, kind: TransactionKind, **variant: str | None
) -> TransactionResponse:
    """Build a complete response in one constructor call.

    Variant-specific fields are passed through rather than assigned after
    construction. Pydantic does not validate on assignment by default, so
    post-construction writes skip the checks the constructor applies, and they
    split a response's definition across two places.
    """
    return TransactionResponse(
        transaction_id=require(
            src.transaction_id, "transactionId", resource="Transaction"
        ),
        kind=kind,
        amount=src.amount,
        currency=src.currency,
        description=src.description,
        posted_at=src.posted_at,
        **variant,
    )


def _build_reversal(src: Transaction) -> TransactionResponse:
    return _base(src, kind="reversal", reverses_transaction_id=src.reversal_of)


def _build_pending_debit(src: Transaction) -> TransactionResponse:
    return _base(src, kind="pending_debit", merchant=src.merchant)


def _build_debit(src: Transaction) -> TransactionResponse:
    return _base(src, kind="debit", merchant=src.merchant)


def _build_credit(src: Transaction) -> TransactionResponse:
    return _base(src, kind="credit")


def _build_fee(src: Transaction) -> TransactionResponse:
    return _base(
        src,
        kind="fee",
        # Required for this variant specifically: a fee the caller cannot
        # categorize is not useful, and the upstream always sends it for fees.
        fee_code=require(src.fee_code, "feeCode", resource="Transaction(FEE)"),
    )


# -- stage 2: rules within a family ---------------------------------------
# ORDER MATTERS. A reversal is also unposted while it settles, so the
# reversal check must come before the pending check. test_transaction_rules.py
# pins that precedence; if you reorder these, that test tells you.
#
# These tables are public (no underscore) because the structural tests inspect
# them, and "private but imported by tests" is a contradiction. They are a
# legitimate part of this module's surface.

DEBIT_RULES: tuple[Rule, ...] = (
    Rule("reversal", lambda s: s.reversal_of is not None, _build_reversal),
    Rule("pending", lambda s: s.posted_at is None, _build_pending_debit),
    Rule("standard", lambda s: True, _build_debit),
)

CREDIT_RULES: tuple[Rule, ...] = (
    Rule("reversal", lambda s: s.reversal_of is not None, _build_reversal),
    Rule("standard", lambda s: True, _build_credit),
)

FEE_RULES: tuple[Rule, ...] = (Rule("standard", lambda s: True, _build_fee),)


def _apply(rules: tuple[Rule, ...], src: Transaction, family: str) -> TransactionResponse:
    for rule in rules:
        if rule.matches(src):
            return rule.build(src)
    # Unreachable while every family ends in a catch-all, which
    # test_catch_all_is_last enforces. Kept as defence in depth: if that
    # invariant is ever broken, this raises rather than returning None.
    raise UpstreamContractError(
        f"no rule matched in family {family}", resource="Transaction"
    )


# -- stage 1: family by tag ------------------------------------------------
# A dict rather than if/elif: adding a family is one line in one place, and a
# missing key raises instead of falling through to whatever came last.

FAMILIES: dict[str, tuple[Rule, ...]] = {
    "DEBIT": DEBIT_RULES,
    "CREDIT": CREDIT_RULES,
    "FEE": FEE_RULES,
}


def map_transaction(src: Transaction) -> TransactionResponse:
    """Map one upstream transaction, dispatching on type then on conditions."""
    if src.type is None:
        raise UpstreamContractError(
            "upstream omitted required field type", resource="Transaction"
        )

    rules = FAMILIES.get(src.type)
    if rules is None:
        # Loud rather than a generic fallback. An unhandled variant should
        # show up the first time one arrives, not as a support ticket.
        raise UpstreamContractError(
            f"unknown transaction type {src.type!r}", resource="Transaction"
        )

    return _apply(rules, src, family=src.type)
