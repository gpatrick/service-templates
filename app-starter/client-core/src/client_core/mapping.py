"""The small shared surface for mapping upstream models onto an app's own.

Deliberately almost nothing. Response models and mappers are per-application
contracts and do not travel between projects; what travels is the error type
and the one helper that raises it.

The error lives here rather than in an application because the FastAPI
exception handlers that map it to 502 are shared, and a handler can only catch
a type it can import.

What is NOT here, on purpose: any Mapper base class, registry, or dispatch
framework. Two of the three consuming applications map unconditionally, and a
plain function is the right shape for that. The one that needs conditional
dispatch defines its own rule table locally; see
examples/api/transaction_rules.py. Fifteen lines copied into a second app
later would still be cheaper than a shared abstraction serving two slightly
different dispatch needs.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import ValidationError

__all__ = ["UpstreamContractError", "require", "maps_upstream"]

T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Any])


class UpstreamContractError(Exception):
    """The upstream sent something an application's contract cannot represent.

    Raised when a field the application declares required came back null or
    absent, or when no mapping rule matched a payload. Either way it is an
    upstream problem rather than a caller problem, so it maps to 502.

    The alternative is letting None propagate into a response that declares
    the field required, which pydantic rejects at serialization time with a
    500 and a stack trace naming the application's own model. Raising here
    names the upstream field instead, which is the thing someone needs.
    """

    def __init__(self, detail: str, *, resource: str | None = None) -> None:
        self.detail = detail
        self.resource = resource
        where = f"{resource}: " if resource else ""
        super().__init__(f"{where}{detail}")


def maps_upstream(fn: F) -> F:
    """Convert pydantic errors raised while building OUR model into 502s.

    A mapper constructs an application response model from upstream values, so
    a type the upstream got wrong surfaces as a ValidationError naming OUR
    model. That is outside the APIError hierarchy and outside every exception
    handler, so it lands as an unhandled 500 blaming this service for someone
    else's payload.

    Example: an application types `created_at` as datetime while the client
    types it `str`. An upstream timestamp of "15/01/2026" parses fine at the
    transport layer and then explodes here.

    A decorator rather than try/except in each mapper, because the next mapper
    someone adds would otherwise have to remember.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ValidationError as exc:
            raise UpstreamContractError(
                f"upstream values did not fit the response model: {exc}",
                resource=fn.__name__,
            ) from exc

    return wrapper  # type: ignore[return-value]


def require(value: T | None, field: str, *, resource: str) -> T:
    """Return value, or raise UpstreamContractError naming the missing field.

    Use for fields the application's contract requires. A field the CLIENT
    model also requires never reaches this: the client raises
    ResponseValidationError while parsing, which already maps to 502. This
    matters for fields the client treats as optional, and as a guard for a
    future `make models` run that turns a required field optional because the
    sample corpus happened to miss the null case.
    """
    if value is None:
        raise UpstreamContractError(
            f"upstream omitted required field {field}", resource=resource
        )
    return value
