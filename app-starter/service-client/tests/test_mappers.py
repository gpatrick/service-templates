"""Tests for the upstream-to-API mapping layer.

Mappers are pure functions, so these need no transport, no client, and no
event loop. They are cheap, which matters because this is where an upstream
change should surface: a regenerated client that renames or drops a field
breaks a mapper, and these tests say exactly which one and how.

Two tests per mapper is the working minimum: a full payload and a minimal one.
The minimal case is the one that catches a field wrongly declared required.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from examples.api import (
    AccountResponse,
    TransferCreateRequest,
    UpstreamContractError,
    map_transaction,
    to_account_response,
    to_transaction_page,
    to_transfer_response,
    to_upstream_transfer_request,
)
from service_client.models.accounts import Account, Transaction, TransactionList
from service_client.models.payments import Transfer

# -- accounts --------------------------------------------------------------


def test_account_maps_every_field():
    src = Account(
        accountId="12345",
        name="Checking",
        status="OPEN",
        currency="USD",
        balance=Decimal("1250.75"),
        availableBalance=Decimal("1200.00"),
    )

    out = to_account_response(src)

    assert isinstance(out, AccountResponse)
    assert out.account_id == "12345"
    assert out.name == "Checking"
    assert out.status == "OPEN"
    assert out.currency == "USD"
    assert out.balance == Decimal("1250.75")
    assert out.available_balance == Decimal("1200.00")


def test_account_maps_minimal_payload():
    """Everything but the id is optional on our side too."""
    out = to_account_response(Account(accountId="1"))

    assert out.account_id == "1"
    assert out.name is None
    assert out.balance is None


def test_account_without_id_is_a_contract_violation():
    """Our contract declares account_id required; the upstream's does not.

    The mapper is where that difference is resolved, and it names the upstream
    field so whoever reads the log knows which system is at fault.
    """
    src = Account.model_construct(account_id=None, name="Checking")

    with pytest.raises(UpstreamContractError) as excinfo:
        to_account_response(src)

    assert "accountId" in excinfo.value.detail
    assert excinfo.value.resource == "Account"


# -- transactions ----------------------------------------------------------


def test_transaction_drops_account_id():
    """The caller supplied it in the path; echoing it per row is noise."""
    src = Transaction(
        transactionId="t1", accountId="a1", type="DEBIT", postedAt="2026-01-15"
    )

    out = map_transaction(src)

    assert not hasattr(out, "account_id")


def test_transaction_page_maps_rows_and_cursor():
    src = TransactionList(
        transactions=[
            Transaction(transactionId="t1", type="DEBIT", postedAt="2026-01-15"),
            Transaction(transactionId="t2", type="CREDIT", postedAt="2026-01-15"),
        ],
        nextCursor="page2",
    )

    out = to_transaction_page(src)

    assert [t.transaction_id for t in out.transactions] == ["t1", "t2"]
    assert out.next_cursor == "page2"


def test_transaction_page_handles_empty_result():
    out = to_transaction_page(TransactionList())

    assert out.transactions == []
    assert out.next_cursor is None


# -- transfers -------------------------------------------------------------


def test_inbound_transfer_request_maps_to_upstream():
    src = TransferCreateRequest(
        from_account_id="a1",
        to_account_id="a2",
        amount=Decimal("25.00"),
        currency="USD",
        description="Rent",
    )

    out = to_upstream_transfer_request(src)

    assert out.from_account_id == "a1"
    assert out.to_account_id == "a2"
    assert out.amount == Decimal("25.00")
    assert out.description == "Rent"


def test_inbound_transfer_request_preserves_decimal_scale():
    """25.00 must not become 25.0 or 25 on the way through."""
    src = TransferCreateRequest(
        from_account_id="a1",
        to_account_id="a2",
        amount=Decimal("25.00"),
        currency="USD",
    )

    out = to_upstream_transfer_request(src)

    assert str(out.amount) == "25.00"


def test_transfer_response_maps_every_field():
    src = Transfer(
        transferId="tr-1",
        fromAccountId="a1",
        toAccountId="a2",
        amount=Decimal("25.00"),
        currency="USD",
        status="PENDING",
        createdAt="2026-01-15T09:30:00Z",
    )

    out = to_transfer_response(src)

    assert out.transfer_id == "tr-1"
    assert out.status == "PENDING"
    assert out.created_at is not None


def test_transfer_response_maps_minimal_payload():
    out = to_transfer_response(Transfer(transferId="tr-1"))

    assert out.transfer_id == "tr-1"
    assert out.status is None


def test_transfer_without_id_is_a_contract_violation():
    """Worse here than on a read: without an id the caller cannot reconcile."""
    src = Transfer.model_construct(transfer_id=None, status="PENDING")

    with pytest.raises(UpstreamContractError) as excinfo:
        to_transfer_response(src)

    assert "transferId" in excinfo.value.detail


# -- serialization ---------------------------------------------------------


def test_money_serializes_as_a_string():
    """A JSON number invites consumers to parse money as a float."""
    out = to_account_response(Account(accountId="1", balance=Decimal("1250.75")))

    dumped = out.model_dump(mode="json")

    assert dumped["balance"] == "1250.75"
    assert isinstance(dumped["balance"], str)


def test_money_serialization_preserves_trailing_zeros():
    out = to_account_response(Account(accountId="1", balance=Decimal("25.00")))

    assert out.model_dump(mode="json")["balance"] == "25.00"


def test_null_money_stays_null_rather_than_becoming_a_string():
    out = to_account_response(Account(accountId="1"))

    assert out.model_dump(mode="json")["balance"] is None


# -- upstream values that do not fit our model ------------------------------


def test_bad_upstream_timestamp_is_a_contract_violation_not_a_crash():
    """Our TransferResponse types created_at as datetime; the client types it
    str. An upstream value that is not ISO-8601 parses fine at the transport
    layer and then fails when we construct our own model.

    Without the maps_upstream decorator this escapes as a raw pydantic
    ValidationError, outside the APIError hierarchy and outside every
    exception handler, landing as a 500 that blames our model for their data.
    """
    src = Transfer(transferId="tr-1", createdAt="15/01/2026")

    with pytest.raises(UpstreamContractError) as excinfo:
        to_transfer_response(src)

    assert "response model" in excinfo.value.detail


def test_valid_iso_timestamp_still_parses():
    out = to_transfer_response(
        Transfer(transferId="tr-1", createdAt="2026-01-15T09:30:00Z")
    )

    assert out.created_at is not None
    assert out.created_at.year == 2026
