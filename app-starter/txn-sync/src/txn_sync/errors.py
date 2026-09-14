"""Failure taxonomy for the sync job.

A scheduled job needs a finer distinction than a web request does. A request
either succeeds or returns a status; a batch run has to decide, per record,
whether to move on, set the record aside, or stop entirely. Those are three
different answers and collapsing them loses a run's worth of work or, worse,
keeps going through a failure that was never going to resolve.

The four outcomes:

  TransientFailure   The record may well succeed later. Leave it out of the
                     checkpoint so the next run picks it up. Counts toward the
                     failure threshold.

  TerminalFailure    This record will fail identically next run: a malformed
                     payload, a rejected field, a contract mismatch. Retrying
                     it forever is worse than setting it aside, so it goes to
                     the dead-letter file and the run continues. Counts toward
                     the threshold.

  FatalFailure       Nothing will succeed: bad credentials, unreachable host,
                     missing configuration. Stop now rather than making the
                     same failing call once per record and burning the
                     threshold to discover what the first failure already
                     said.

  RunAborted         The failure rate crossed the configured threshold. Not a
                     failure of any one record; a judgment that continuing is
                     unwise. Distinct from FatalFailure because the checkpoint
                     is still valid and a re-run resumes cleanly.

`classify_failure` maps the client library's exceptions onto these. It is a
function rather than a dict so the reasoning for each case can live next to
the case.
"""

from __future__ import annotations

__all__ = [
    "SyncError",
    "TransientFailure",
    "TerminalFailure",
    "FatalFailure",
    "RunAborted",
    "classify_failure",
]


class SyncError(Exception):
    """Base class for everything this job raises deliberately."""


class TransientFailure(SyncError):
    """May succeed on a later attempt. Not checkpointed; retried next run."""


class TerminalFailure(SyncError):
    """Will fail identically next run. Dead-lettered; the run continues."""


class FatalFailure(SyncError):
    """Nothing will succeed. The run stops immediately."""


class RunAborted(SyncError):
    """The failure rate crossed the threshold. The run stops; state is intact."""


def classify_failure(exc: BaseException) -> type[SyncError]:
    """Decide how a raised exception should be handled.

    Imports live inside the function so this module stays importable without
    service-client on the path. That matters for the core-does-not-depend-on-
    adapters property the layering test enforces: errors.py is imported by
    runner.py, which must remain testable with in-memory fakes alone.
    """
    if isinstance(exc, SyncError):
        return type(exc)

    try:
        from service_client import (
            APIConnectionError,
            APIStatusError,
            AuthError,
            NotFoundError,
            RateLimitError,
            ResponseValidationError,
            ServerError,
            TokenFetchError,
            UpstreamContractError,
        )
    except ImportError:  # pragma: no cover - only when running core-only tests
        return TransientFailure

    # Credentials are wrong or the token endpoint is refusing us. Every
    # subsequent call fails the same way, so stop rather than spend the
    # threshold proving it.
    if isinstance(exc, (AuthError, TokenFetchError)):
        return FatalFailure

    # No response was received, or the server is unhealthy, or we are being
    # rate limited past what the client's own retry budget could absorb. All
    # plausibly better later.
    if isinstance(exc, (APIConnectionError, ServerError, RateLimitError)):
        return TransientFailure

    # A 404 on one record is that record's problem. It is terminal rather
    # than fatal because a missing account among several should not take the
    # run down; if every account is missing, the threshold catches it.
    if isinstance(exc, NotFoundError):
        return TerminalFailure

    # The upstream sent a 2xx body our models do not describe, or a value the
    # transform cannot represent. Both fail identically on a re-run until the
    # models are regenerated, so dead-letter and keep the payload.
    if isinstance(exc, (ResponseValidationError, UpstreamContractError)):
        return TerminalFailure

    # Remaining 4xx: the request itself is wrong. Same body next run, same
    # rejection.
    if isinstance(exc, APIStatusError):
        return TerminalFailure

    # Anything unrecognized is treated as terminal rather than transient. An
    # unknown error retried forever is a silent infinite loop across runs; an
    # unknown error dead-lettered is a line in a file someone will read.
    return TerminalFailure
