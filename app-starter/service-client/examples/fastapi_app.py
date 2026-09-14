"""FastAPI wiring for the Accounts and Payments clients.

Call path: gateway -> this app -> AccountsClient / PaymentsClient -> upstream.

Five things this file exists to get right:

  1. One client per service for the whole process, created in lifespan. A
     client per request discards connection pooling and refetches a token on
     every call.
  2. ONE auth object shared by both clients. The two services accept the same
     credential, so sharing gives one token cache and one refresh instead of
     two. The lock inside RefreshingBearerAuth makes concurrent access safe.
  3. SEPARATE retry policies and separate connection pools. Accounts reads
     are idempotent and cheap to retry; Payments moves money. Separate pools
     also mean a burst of Accounts traffic cannot consume every connection
     and stall transfers behind it.
  4. Upstream errors mapped to sensible status codes for OUR caller. A 401
     between us and the upstream is not a 401 between the gateway and us.
  5. Upstream MODELS mapped to our own response models. Endpoints never return
     a client model directly, so regenerating the client cannot silently
     change this API's contract. See examples/api/.

Run with:  uvicorn examples.fastapi_app:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_client import (
    ACCOUNTS_RETRY,
    PAYMENTS_RETRY,
    AccountsClient,
    APIConnectionError,
    APIStatusError,
    NotFoundError,
    PaymentsClient,
    RateLimitError,
    ResponseValidationError,
    TokenFetchError,
    UpstreamContractError,
    client_credentials_auth,
)

from .api import (
    AccountResponse,
    TransactionPage,
    TransferCreateRequest,
    TransferResponse,
    to_account_response,
    to_transaction_page,
    to_transfer_response,
    to_upstream_transfer_request,
)

# Per-request timeout. Kept well under the gateway's limit so the retry
# budget (see RetryPolicy.max_elapsed) has room to work. Re-check both if the
# gateway timeout changes.
UPSTREAM_TIMEOUT = 6.0

# Split the connection allowance between the two services rather than giving
# each the default 50, which would open 100 against one host. Accounts gets
# the larger share; transfer volume is far lower.
ACCOUNTS_CONNECTIONS = 40
PAYMENTS_CONNECTIONS = 10


class Settings(BaseSettings):
    """Configuration, validated at startup.

    Read inside lifespan rather than at import, deliberately. At import time
    the process may not have its environment yet, and a test that wants to
    supply values has already lost the chance. At startup the values are read
    once, validated together, and every problem is reported in one message.

    Real environment variables win over .env, so a container or systemd unit
    overrides the developer file without anyone deleting anything.
    """

    model_config = SettingsConfigDict(
        env_prefix="SERVICE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_url: str
    token_url: str
    client_id: str
    client_secret: SecretStr


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Missing configuration fails here, before the server accepts traffic.
    # That is the point: better a clean startup failure than a service that
    # boots green and 500s on every request.
    settings = Settings()

    auth = client_credentials_auth(
        settings.token_url,
        client_id=settings.client_id,
        # get_secret_value at the single point of use. Everywhere else the
        # value reprs as ********, so a stray log line cannot leak it.
        client_secret=settings.client_secret.get_secret_value(),
    )

    app.state.accounts = AccountsClient(
        settings.base_url,
        auth=auth,
        timeout=UPSTREAM_TIMEOUT,
        retry=ACCOUNTS_RETRY,
        max_connections=ACCOUNTS_CONNECTIONS,
    )
    app.state.payments = PaymentsClient(
        settings.base_url,
        auth=auth,
        timeout=UPSTREAM_TIMEOUT,
        retry=PAYMENTS_RETRY,
        max_connections=PAYMENTS_CONNECTIONS,
    )

    try:
        yield
    finally:
        await app.state.accounts.aclose()
        await app.state.payments.aclose()


app = FastAPI(lifespan=lifespan)


# -- dependencies ----------------------------------------------------------
# Endpoints never read app.state directly. Going through a dependency is what
# lets tests swap in a client wired to a MockTransport:
#
#     app.dependency_overrides[get_accounts] = lambda: fake_accounts


def get_accounts(request: Request) -> AccountsClient:
    return request.app.state.accounts


def get_payments(request: Request) -> PaymentsClient:
    return request.app.state.payments


# The Annotated form rather than a Depends() default: endpoints then have no
# default values, so they stay directly callable in a unit test, and each
# dependency is declared once instead of repeated in every signature.
AccountsDep = Annotated[AccountsClient, Depends(get_accounts)]
PaymentsDep = Annotated[PaymentsClient, Depends(get_payments)]


# -- error mapping ---------------------------------------------------------
# Registered once, so no endpoint has to remember to handle these. Handlers
# key on exception type, not on which client raised, so both services share
# them for free.


@app.exception_handler(NotFoundError)
async def handle_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "resource not found"})


@app.exception_handler(RateLimitError)
async def handle_rate_limit(request: Request, exc: RateLimitError) -> JSONResponse:
    # Pass the pressure downstream rather than hiding it as a 502.
    return JSONResponse(status_code=429, content={"detail": "upstream rate limited"})


@app.exception_handler(TokenFetchError)
async def handle_token_failure(request: Request, exc: TokenFetchError) -> JSONResponse:
    # We could not authenticate to the upstream. That is our configuration
    # problem, not the caller's, so it must not surface as a 401.
    return JSONResponse(status_code=503, content={"detail": "upstream auth failed"})


@app.exception_handler(APIConnectionError)
async def handle_connection(request: Request, exc: APIConnectionError) -> JSONResponse:
    return JSONResponse(status_code=504, content={"detail": "upstream unreachable"})


@app.exception_handler(ResponseValidationError)
async def handle_bad_upstream_body(
    request: Request, exc: ResponseValidationError
) -> JSONResponse:
    # A 2xx body that did not match the client's model. Almost always means
    # the models need regenerating from fresh samples. Not our caller's
    # problem and not our bug, so 502 rather than a 500.
    return JSONResponse(status_code=502, content={"detail": "upstream error"})


@app.exception_handler(UpstreamContractError)
async def handle_contract_violation(
    request: Request, exc: UpstreamContractError
) -> JSONResponse:
    # The upstream omitted a field our contract declares required. Ours is
    # intact; theirs is not, so this is 502 rather than a 500 blaming us.
    # The field name goes to logs, not to the caller.
    return JSONResponse(status_code=502, content={"detail": "upstream error"})


@app.exception_handler(APIStatusError)
async def handle_upstream(request: Request, exc: APIStatusError) -> JSONResponse:
    # Catch-all for everything above, including 401 and 403. Never forward the
    # upstream status: it would tell our caller to re-authenticate with the
    # wrong system. The body is not echoed either; it may leak internals.
    return JSONResponse(status_code=502, content={"detail": "upstream error"})


# -- accounts --------------------------------------------------------------


@app.get("/accounts/{account_id}", response_model=AccountResponse)
async def read_account(account_id: str, accounts: AccountsDep) -> AccountResponse:
    return to_account_response(await accounts.get_account(account_id))


@app.get("/accounts/{account_id}/transactions", response_model=TransactionPage)
async def read_transactions(
    account_id: str,
    accounts: AccountsDep,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> TransactionPage:
    # The cursor is passed through rather than drained server-side with
    # iter_transactions. Draining would turn an unbounded upstream result into
    # an unbounded response and an unbounded memory footprint per request.
    page = await accounts.list_transactions(
        account_id,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        cursor=cursor,
    )
    return to_transaction_page(page)


# -- payments --------------------------------------------------------------


@app.post("/transfers", response_model=TransferResponse, status_code=201)
async def create_transfer(
    transfer: TransferCreateRequest, payments: PaymentsDep
) -> TransferResponse:
    # A malformed body is rejected with 422 by TransferCreateRequest before
    # any upstream call happens. No retry wrapper here either, deliberately:
    # if this times out, surface 504 and let the caller decide rather than
    # repeating a request that may already have moved money.
    result = await payments.create_transfer(to_upstream_transfer_request(transfer))
    return to_transfer_response(result)
