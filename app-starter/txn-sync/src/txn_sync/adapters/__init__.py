"""Concrete implementations of the ports in contracts.py.

Nothing in the core imports this package. The dependency runs one way only:
adapters import the core, never the reverse. tests/test_layering.py enforces
it, because the property is easy to break with one convenience import and the
consequence is that the core stops being testable without a network.

Imports here are lazy, inside functions, for the same reason the modules are
split: `memory` must be importable without service-client present.
"""

__all__: list[str] = []
