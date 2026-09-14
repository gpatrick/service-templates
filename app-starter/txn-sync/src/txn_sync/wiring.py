"""Builds the real adapters from configuration. The only place that does.

Everything else in the project takes its dependencies as constructor
arguments, which is what makes it testable. This module is where that stops
and something has to actually decide which classes to instantiate.

Imports of the HTTP adapters are deliberately inside the functions. That keeps
`import txn_sync` working on a machine where service-client is not installed,
which matters for running the core's tests and for reading the code before the
dependency is available.

TWO AUTH OBJECTS, NOT ONE. The source and the destination are separate
systems with separate credentials. service-client's own docstring describes
sharing one auth object between two clients, and that is right there, because
Accounts and Payments are two services behind one gateway. It is wrong here:
sharing would send the source's token to the destination, which at best fails
with a 401 and at worst succeeds against a system that should not have
accepted it.
"""

from __future__ import annotations

from .config import SyncConfig
from .contracts import TransactionDestination, TransactionSource

__all__ = ["build_source", "build_destination", "close_quietly"]


def build_source(config: SyncConfig) -> TransactionSource:
    from service_client import client_credentials_auth

    from .adapters.source_http import make_http_source

    auth = client_credentials_auth(
        config.source_token_url,
        client_id=config.source_client_id,
        client_secret=config.source_client_secret,
    )
    return make_http_source(base_url=config.source_base_url, auth=auth)


def build_destination(config: SyncConfig) -> TransactionDestination:
    """Return the real destination, or a DryRunDestination when rehearsing.

    The dry-run substitution happens here rather than in the runner so that
    there is no code path in which a dry run holds a live destination client
    at all. A guard in the runner would be one `if` away from posting for
    real; not constructing the client is a stronger guarantee.
    """
    if config.dry_run:
        from .adapters.memory import DryRunDestination

        return DryRunDestination()

    from service_client import client_credentials_auth

    from .adapters.destination_http import make_http_destination

    auth = client_credentials_auth(
        config.destination_token_url,
        client_id=config.destination_client_id,
        client_secret=config.destination_client_secret,
    )
    return make_http_destination(
        base_url=config.destination_base_url,
        auth=auth,
        source_system=config.source_system,
    )


async def close_quietly(*closeables: object) -> None:
    """Close every port, letting no failure hide another.

    A close that raises must not prevent the other close from running, and
    neither must replace the run's real outcome. The job's exit code is about
    what happened to the records, not about whether a socket shut down
    tidily.
    """
    for closeable in closeables:
        aclose = getattr(closeable, "aclose", None)
        if aclose is None:
            continue
        try:
            await aclose()
        except Exception:  # noqa: BLE001 - closing must not mask the outcome
            pass
