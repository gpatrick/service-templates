from __future__ import annotations

import httpx
import pytest

BASE_URL = "https://cls.example.com"


@pytest.fixture
def make_client():
    """Build any cls_client client wired to a MockTransport handler.

    Injecting through `http_client` keeps the real client code in play: same
    paging, same validation, same error mapping. Only the transport is fake.
    """

    def _make(cls, handler, **kwargs):
        return cls(
            BASE_URL,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url=BASE_URL,
                headers={"Authorization": "Bearer test-token"},
            ),
            **kwargs,
        )

    return _make
