from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import make_transaction

from txn_sync.transform import DESCRIPTION_MAX_LEN, TransformError, transform


def test_identity_fields_carry_through():
    source = make_transaction("t1", "acct-1")
    result = transform(source)
    assert result.account_id == "acct-1"
    assert result.source_transaction_id == "t1"
    assert result.business_date == source.business_date


def test_the_source_id_is_carried_for_reconciliation():
    """Without an external reference, a lost response leaves a record that
    cannot be looked up in the destination afterwards."""
    assert transform(make_transaction("t42")).source_transaction_id == "t42"


def test_amount_stays_decimal():
    """Never float. The error compounds across a summed batch."""
    result = transform(make_transaction(amount="10.10"))
    assert isinstance(result.amount, Decimal)
    assert result.amount == Decimal("10.10")


def test_currency_is_uppercased():
    assert transform(make_transaction(currency="usd")).currency == "USD"


def test_missing_currency_is_a_transform_error():
    with pytest.raises(TransformError, match="currency"):
        transform(make_transaction(currency=""))


def test_long_descriptions_are_truncated():
    """Destinations usually cap free text and reject overruns with a 400 that
    names the field but not the limit."""
    long = "x" * (DESCRIPTION_MAX_LEN + 50)
    result = transform(make_transaction(description=long))
    assert result.description is not None
    assert len(result.description) == DESCRIPTION_MAX_LEN


def test_a_short_description_is_untouched():
    assert transform(make_transaction(description="coffee")).description == "coffee"


def test_a_missing_description_stays_missing():
    assert transform(make_transaction(description=None)).description is None


def test_the_transform_is_total():
    """Every record either maps or raises. There is no silent third outcome.

    A transform that can return None is a place for records to disappear
    without appearing in any skip count.
    """
    result = transform(make_transaction())
    assert result is not None


def test_the_transform_does_not_mutate_its_input():
    source = make_transaction(description="x" * 500)
    before = source.description
    transform(source)
    assert source.description == before
