"""AccountsClient. CUSTOMIZE THIS FILE.

Everything here is specific to the Accounts service. transport.py and auth.py
are generic and should not need to change.

Rules that keep this file maintainable:

  * Keep methods to one line. All behavior (retries, error mapping,
    validation) lives in BaseClient._request, so every endpoint gets it for
    free and none of them can forget.
  * Required upstream parameters stay required in the signature. Do not give
    them defaults to make calling easier; that moves the failure from the
    call site to runtime.
  * Optional parameters are keyword-only with a None default. BaseClient
    drops None params before sending, so they never serialize as "None".
  * Return a model, never a dict. Callers depend on these signatures; a dict
    return is impossible to change later without breaking them.

ASSUMPTIONS MADE HERE. Confirm each against the real API:

  * GET /accounts/{accountId}              -> a single account
  * GET /accounts/{accountId}/transactions -> a list, probably paginated

  The optional query parameters on list_transactions are a guess at a common
  shape (date range plus cursor pagination). Replace them with the real ones.
  An unrecognized query parameter is usually ignored rather than rejected, so
  a wrong guess here fails quietly and you get unfiltered results that look
  plausible.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from client_core import BaseClient

from .models.accounts import Account, Transaction, TransactionList

__all__ = ["AccountsClient"]


class AccountsClient(BaseClient):
    """Typed client for the Accounts service.

    async with AccountsClient(base_url, auth=auth) as accounts:
        account = await accounts.get_account("12345")
    """

    # -- accounts ----------------------------------------------------------

    async def get_account(self, account_id: str) -> Account:
        return await self._get(f"/accounts/{account_id}", Account)

    # -- transactions ------------------------------------------------------

    async def list_transactions(
        self,
        account_id: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> TransactionList:
        return await self._get(
            f"/accounts/{account_id}/transactions",
            TransactionList,
            startDate=start_date,
            endDate=end_date,
            limit=limit,
            cursor=cursor,
        )

    async def iter_transactions(
        self,
        account_id: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        page_size: int | None = None,
    ) -> AsyncIterator[Transaction]:
        """Yield every transaction, following pagination.

        Lives here rather than at the call site so two projects do not each
        write their own paging loop and get it subtly different. Remove it if
        the endpoint turns out not to paginate.

            async for txn in accounts.iter_transactions("12345"):
                ...
        """
        cursor: str | None = None
        while True:
            page = await self.list_transactions(
                account_id,
                start_date=start_date,
                end_date=end_date,
                limit=page_size,
                cursor=cursor,
            )
            for transaction in page.transactions:
                yield transaction

            cursor = page.next_cursor
            if not cursor:
                return
