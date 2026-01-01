import asyncio
import pytest

from types import SimpleNamespace
from fastapi import WebSocketDisconnect

import server.api as api
from server.websocket_manager import WebSocketManager


class FakeWS:
    def __init__(self, queries, messages):
        # queries: dict of query params
        self.query_params = queries
        self._messages = list(messages)
        self.accepted = False
        self.closed = False
        self.close_code = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=None):
        self.closed = True
        self.close_code = code

    async def receive_json(self):
        if not self._messages:
            raise WebSocketDisconnect()
        return self._messages.pop(0)

    async def send_json(self, obj):
        # noop for tests
        return


@pytest.mark.asyncio
async def test_websocket_endpoint_missing_params_closes():
    fake = FakeWS({}, [])
    await api.websocket_endpoint(fake)
    assert fake.closed


@pytest.mark.asyncio
async def test_websocket_endpoint_register_and_status(monkeypatch):
    # Monkeypatch verify_passkey to accept
    async def fake_verify(u, p):
        return None

    monkeypatch.setattr(api, "verify_passkey", fake_verify)

    # Replace module ws_manager with a fresh manager to observe adds/removes
    fake_mgr = WebSocketManager()
    monkeypatch.setattr(api, "ws_manager", fake_mgr)

    # capture orchestrator calls
    called = {}

    async def fake_handle(username, filename, offset, receive_time=None):
        called["status"] = (username, filename, offset)

    monkeypatch.setattr(api, "orchestrator", SimpleNamespace(handle_client_status_update=fake_handle))

    # prepare messages: register then status_update then disconnect
    msgs = [
        {"type": "register", "authorized_clients": [{"title": "Local", "id": "m1"}]},
        {"type": "status_update", "filename": "f.mp4", "current_offset": 1234},
    ]
    fake = FakeWS({"username": "u", "passkey": "p"}, msgs)

    await api.websocket_endpoint(fake)

    # ensure websocket was accepted and orchestrator was called for status
    assert fake.accepted
    assert "status" in called