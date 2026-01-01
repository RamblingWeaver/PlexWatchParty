import pytest
from types import SimpleNamespace

from fastapi import HTTPException

import server.api as api


@pytest.mark.asyncio
async def test_verify_passkey_unconfigured():
    api.settings = SimpleNamespace(passkey_validation_url="")
    with pytest.raises(HTTPException) as exc:
        await api.verify_passkey("u", "k")
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_verify_passkey_missing_username_or_passkey():
    api.settings = SimpleNamespace(passkey_validation_url="http://x")
    with pytest.raises(HTTPException) as exc:
        await api.verify_passkey("", "k")
    assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc2:
        await api.verify_passkey("u", "")
    assert exc2.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_passkey_success(monkeypatch):
    api.settings = SimpleNamespace(passkey_validation_url="http://x")

    class FakeResp:
        def __init__(self):
            self.status_code = 200

        def json(self):
            return {"valid": True}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            return FakeResp()

    monkeypatch.setattr(api, "httpx", SimpleNamespace(AsyncClient=FakeClient))

    # Should not raise
    await api.verify_passkey("u", "k")
