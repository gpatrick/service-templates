"""Tests for conditional transaction mapping.

Three kinds of test here, and the last two are the ones usually skipped:

  1. One per variant, asserting the mapped shape.
  2. Precedence: which rule wins when two match. Without this, reordering the
     list during a refactor silently changes behavior.
  3. Structure: that the rule tables themselves hold. A catch-all inserted
     anywhere but last disables everything below it, and nothing else catches
     that.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from client_core import UpstreamContractError

from examples.api import map_transaction
from examples.api.transaction_rules import (
    CREDIT_RULES,
    DEBIT_RULES,
    FAMILIES,
    FEE_RULES,
)
from service_client.models.accounts import Transaction


def a_txn(**overrides) -> Transaction:
    fields = {
        "transactionId": "t1",
        "type": "DEBIT",
        "amount": Decimal("-42.10"),
        "currency": "USD",
        "postedAt": "2026-01-15T09:30:00Z",
    }
    fields.update(overrides)
    return Transaction(**fields)


# -- one per variant -------------------------------------------------------


def test_standard_debit():
    out = map_transaction(a_txn(merchant="Coffee Shop"))

    assert out.kind == "debit"
    assert out.merchant == "Coffee Shop"
    assert out.amount == Decimal("-42.10")
    assert out.fee_code is None


def test_pending_debit_has_no_posted_at():
    out = map_transaction(a_txn(postedAt=None, merchant="Coffee Shop"))

    assert out.kind == "pending_debit"
    assert out.posted_at is None
    assert out.merchant == "Coffee Shop"


def test_reversal_carries_the_reversed_id():
    out = map_transaction(a_txn(reversalOf="t0"))

    assert out.kind == "reversal"
    assert out.reverses_transaction_id == "t0"


def test_credit():
    out = map_transaction(a_txn(type="CREDIT", amount=Decimal("100.00")))

    assert out.kind == "credit"
    assert out.merchant is None


def test_fee_requires_a_fee_code():
    out = map_transaction(a_txn(type="FEE", feeCode="OD01"))

    assert out.kind == "fee"
    assert out.fee_code == "OD01"


def test_fee_without_a_code_is_a_contract_violation():
    """Required for this variant only; a fee the caller cannot categorize is
    not useful, and the upstream always sends it for fees."""
    with pytest.raises(UpstreamContractError) as excinfo:
        map_transaction(a_txn(type="FEE"))

    assert "feeCode" in excinfo.value.detail


# -- precedence ------------------------------------------------------------


def test_reversal_beats_pending():
    """A reversal is also unposted while it settles, so both rules match.

    This pins which one wins. If someone reorders DEBIT_RULES, this fails
    rather than the behavior changing silently.
    """
    out = map_transaction(a_txn(postedAt=None, reversalOf="t0"))

    assert out.kind == "reversal"


def test_reversal_beats_standard_on_credits_too():
    out = map_transaction(a_txn(type="CREDIT", reversalOf="t0"))

    assert out.kind == "reversal"


# -- nothing defaults silently --------------------------------------------


def test_unknown_type_raises():
    """An unhandled variant must show up the first time one arrives, not as a
    support ticket three weeks later."""
    with pytest.raises(UpstreamContractError) as excinfo:
        map_transaction(a_txn(type="INTEREST_ACCRUAL"))

    assert "INTEREST_ACCRUAL" in excinfo.value.detail


def test_missing_type_raises():
    with pytest.raises(UpstreamContractError) as excinfo:
        map_transaction(a_txn(type=None))

    assert "type" in excinfo.value.detail


# -- the rule tables themselves --------------------------------------------


@pytest.mark.parametrize(
    "name,rules",
    [("DEBIT", DEBIT_RULES), ("CREDIT", CREDIT_RULES), ("FEE", FEE_RULES)],
)
def test_catch_all_is_last(name, rules):
    """A rule inserted after the catch-all is dead code.

    Nothing else catches that: every existing test still passes, and the new
    variant silently maps as standard.
    """
    catch_alls = [i for i, r in enumerate(rules) if r.matches(a_txn())]
    assert catch_alls, f"{name} has no rule matching a plain transaction"
    assert rules[-1].name == "standard", f"{name} catch-all is not last"


def test_rule_names_are_unique_within_a_family():
    """Duplicate names make the error messages ambiguous."""
    for family, rules in FAMILIES.items():
        names = [r.name for r in rules]
        assert len(names) == len(set(names)), f"duplicate rule name in {family}"


def test_every_family_is_reachable():
    """A family in the dict with no rules would raise on every payload."""
    for family, rules in FAMILIES.items():
        assert rules, f"{family} has no rules"
