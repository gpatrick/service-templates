"""The layering rule, enforced rather than documented.

The core must not import either adapter, and must not import the HTTP client
library at all. That property is what makes the pipeline testable without a
network and portable to a machine where the real systems exist, and it is one
convenience import away from being lost. A comment does not survive a busy
afternoon; a failing test does.

These run in a subprocess because import side effects are global: once any
test in this session has imported the adapters, `sys.modules` can no longer
tell you whether the core pulled them in.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

CORE_MODULES = [
    "txn_sync.records",
    "txn_sync.contracts",
    "txn_sync.keys",
    "txn_sync.checkpoint",
    "txn_sync.selection",
    "txn_sync.transform",
    "txn_sync.report",
    "txn_sync.errors",
    "txn_sync.config",
    "txn_sync.runner",
]

FORBIDDEN = [
    "service_client",
    "txn_sync.adapters.source_http",
    "txn_sync.adapters.destination_http",
]


def imports_after(module: str) -> set[str]:
    script = textwrap.dedent(
        f"""
        import importlib, sys, json
        importlib.import_module({module!r})
        print(json.dumps(sorted(sys.modules)))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    import json

    return set(json.loads(result.stdout))


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_modules_do_not_import_the_adapters_or_the_http_client(module: str):
    loaded = imports_after(module)
    leaked = sorted(name for name in FORBIDDEN if name in loaded)
    assert not leaked, (
        f"{module} pulled in {leaked}. The core must depend only on contracts.py; "
        f"see the module docstring in contracts.py for why."
    )


def test_the_package_root_is_importable_without_service_client():
    """Someone reading the code, or running the core's tests, should not need
    the client library installed first."""
    loaded = imports_after("txn_sync")
    assert "service_client" not in loaded


def test_the_memory_adapter_is_importable_without_service_client():
    """It is used by the dry-run path and by the client's own future tests."""
    loaded = imports_after("txn_sync.adapters.memory")
    assert "service_client" not in loaded


def test_the_http_adapters_do_import_the_client_library():
    """The inverse check, so the test above cannot pass by the adapters having
    quietly stopped working."""
    loaded = imports_after("txn_sync.adapters.destination_http")
    assert "service_client" in loaded
