"""Tests for the Accounts service. API-SPECIFIC, expect to rewrite these.

Unlike the transport and auth suites, everything here depends on the upstream
API's actual shape. Each test pins one fact, so a bad model regeneration or a
misread spec fails here rather than in production.

Right now these encode ASSUMPTIONS, not confirmed behavior. As real samples
are recorded, update each test to match what the API returns and delete any
that describe something it does not do.
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from service_client.models.accounts import Account

# -- accounts --------------------------------------------------------------


async def test_get_account_builds_expected_path(make_accounts):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/accounts/12345"
        return httpx.Response(
            200,
            json={
                "accountId": "12345",
                "name": "Checking",
                "status": "OPEN",
                "currency": "USD",
                "balance": "1250.75",
            },
        )

    async with make_accounts(handler) as accounts:
        account = await accounts.get_account("12345")

    assert isinstance(account, Account)
    assert account.account_id == "12345"
    assert account.status == "OPEN"


async def test_amounts_are_decimal_not_float(make_accounts):
    """Money must survive the round trip exactly.

    0.1 + 0.2 != 0.3 in binary floating point, and the error compounds across
    a summed transaction list. If this fails, a Decimal annotation became a
    float, most likely from a regeneration where the API sent a JSON number.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accountId": "1", "balance": "1250.75"})

    async with make_accounts(handler) as accounts:
        account = await accounts.get_account("1")

    assert account.balance == Decimal("1250.75")
    assert not isinstance(account.balance, float)


async def test_wire_names_map_to_python_names(make_accounts):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accountId": "1", "availableBalance": "10.00"})

    async with make_accounts(handler) as accounts:
        account = await accounts.get_account("1")

    assert account.account_id == "1"
    assert account.available_balance == Decimal("10.00")


async def test_absent_optional_fields_parse(make_accounts):
    """A minimal response must not fail validation."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accountId": "1"})

    async with make_accounts(handler) as accounts:
        account = await accounts.get_account("1")

    assert account.name is None
    assert account.balance is None


async def test_null_fields_parse(make_accounts):
    """Fields the spec marks required may still come back null.

    Models are generated from recorded samples specifically so this works. If
    this starts failing, someone regenerated from the published spec instead
    of the sample corpus.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accountId": "1", "name": None, "balance": None})

    async with make_accounts(handler) as accounts:
        account = await accounts.get_account("1")

    assert account.name is None
    assert account.balance is None


# -- transactions ----------------------------------------------------------


async def test_list_transactions_builds_nested_path(make_accounts):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/accounts/12345/transactions"
        return httpx.Response(200, json={"transactions": []})

    async with make_accounts(handler) as accounts:
        page = await accounts.list_transactions("12345")

    assert page.transactions == []


async def test_transaction_filters_use_wire_param_names(make_accounts):
    """The signature is snake_case; the query string is not."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["startDate"] == "2026-01-01"
        assert request.url.params["endDate"] == "2026-01-31"
        assert request.url.params["limit"] == "50"
        return httpx.Response(200, json={"transactions": []})

    async with make_accounts(handler) as accounts:
        await accounts.list_transactions(
            "1", start_date="2026-01-01", end_date="2026-01-31", limit=50
        )


async def test_unset_filters_are_not_sent(make_accounts):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "startDate" not in request.url.params
        assert "cursor" not in request.url.params
        return httpx.Response(200, json={"transactions": []})

    async with make_accounts(handler) as accounts:
        await accounts.list_transactions("1", limit=10)


async def test_iter_transactions_follows_pagination(make_accounts):
    pages = [
        {
            "transactions": [{"transactionId": "t1"}, {"transactionId": "t2"}],
            "nextCursor": "page2",
        },
        {"transactions": [{"transactionId": "t3"}], "nextCursor": None},
    ]
    seen_cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        seen_cursors.append(cursor)
        return httpx.Response(200, json=pages[0] if cursor is None else pages[1])

    async with make_accounts(handler) as accounts:
        ids = [t.transaction_id async for t in accounts.iter_transactions("1")]

    assert ids == ["t1", "t2", "t3"]
    assert seen_cursors == [None, "page2"]


async def test_iter_transactions_stops_without_cursor(make_accounts):
    """A single page must not loop forever."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"transactions": [{"transactionId": "t1"}]})

    async with make_accounts(handler) as accounts:
        ids = [t.transaction_id async for t in accounts.iter_transactions("1")]

    assert ids == ["t1"]
    assert len(calls) == 1


async def test_iter_transactions_passes_filters_to_every_page(make_accounts):
    """Filters must not be dropped after the first page."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("startDate"))
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(
                200, json={"transactions": [{"transactionId": "t1"}], "nextCursor": "p2"}
            )
        return httpx.Response(200, json={"transactions": [{"transactionId": "t2"}]})

    async with make_accounts(handler) as accounts:
        async for _ in accounts.iter_transactions("1", start_date="2026-01-01"):
            pass

    assert seen == ["2026-01-01", "2026-01-01"]
