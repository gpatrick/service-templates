"""Run app.main() against a mock CLS. No network, no credentials, no backend.

    python demo_mock.py

Every AsyncClient the app builds gets an httpx.MockTransport, so the REAL
client code runs: the two-step handshake, the cookie, pagination, validation
and error mapping. Only the socket is fake.

The mock token endpoint refuses to issue a token unless the session cookie is
present, so a successful run also proves the handshake carried it.

Delete this file once you are pointed at the real CLS. The same technique
belongs in your test suite; see cls-client/tests/conftest.py.
"""
import asyncio
import os

import httpx

# Dummy config, so this demo needs no .env and no credentials. The mock server
# below accepts anything; only the SHAPE of the handshake is exercised.
os.environ.setdefault("CLS_BASE_URL", "https://cls.invalid")
os.environ.setdefault("CLS_AUTH_URL", "https://cls.invalid")
os.environ.setdefault("CLS_CLIENT_ID", "demo")
os.environ.setdefault("CLS_CLIENT_SECRET", "demo")

real_init = httpx.AsyncClient.__init__

def handler(request):
    p = request.url.path
    if p == "/auth/session":
        return httpx.Response(200, json={"ok": True},
                              headers={"set-cookie": "CLSSESSION=x; Path=/"})
    if p == "/auth/token":
        if "CLSSESSION" not in request.headers.get("cookie", ""):
            return httpx.Response(401, json={"error": "no session"})
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 900})
    if p == "/accounts/12345":
        return httpx.Response(200, json={"accountId": "12345", "name": "Checking"})
    if p.endswith("/transactions"):
        cur = request.url.params.get("cursor")
        if cur is None:
            return httpx.Response(200, json={"transactions": [{"transactionId": "t1"}],
                                             "nextCursor": "c2"})
        return httpx.Response(200, json={"transactions": [{"transactionId": "t2"}],
                                         "nextCursor": None})
    return httpx.Response(404, json={"error": p})

def patched(self, *a, **kw):
    kw["transport"] = httpx.MockTransport(handler)
    real_init(self, *a, **kw)

httpx.AsyncClient.__init__ = patched

# Imported AFTER the patch, deliberately: app builds its clients at call time,
# but importing it first would still be fragile if that ever changes.
import app  # noqa: E402

asyncio.run(app.main())
