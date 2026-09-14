"""PaymentsClient. CUSTOMIZE THIS FILE.

Everything here is specific to the Payments service. Same rules as
accounts.py: one thin method per operation, required parameters stay
required, return models rather than dicts.

ASSUMPTIONS MADE HERE. Confirm each against the real API:

  * POST /transfers -> creates a transfer

  If /transfers also supports GET for listing, or GET /transfers/{id} for
  read-back, add those. A money-movement resource you can create but never
  confirm is unusual and worth checking before the demo: without read-back
  there is no way to reconcile a create whose response was lost.

RETRY BEHAVIOR IS LOAD-BEARING HERE. See create_transfer.
"""

from __future__ import annotations

from client_core import BaseClient

from .models.payments import Transfer, TransferRequest

__all__ = ["PaymentsClient"]


class PaymentsClient(BaseClient):
    """Typed client for the Payments service.

        async with PaymentsClient(base_url, auth=auth, retry=PAYMENTS_RETRY) as payments:
            transfer = await payments.create_transfer(request)

    Construct this with a stricter RetryPolicy than the Accounts client. See
    DECISIONS.md; the short version is that Accounts reads are idempotent and
    cheap to retry while anything here moves money.
    """

    async def create_transfer(self, transfer: TransferRequest) -> Transfer:
        """Create a transfer.

        NOT retried, and that default must stay. A retried POST after a
        timeout can move money twice, because the request may have succeeded
        with only the response lost. RetryPolicy.retry_non_idempotent is
        False by default; do not enable it on this client.

        If the API accepts an idempotency key, send one and reuse the same
        value across any retry the CALLER performs. Add the parameter once
        you know the header name the API expects.
        """
        return await self._post("/transfers", Transfer, body=transfer)
