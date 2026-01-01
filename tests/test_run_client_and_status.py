import sys
import os
import sys
import os
import asyncio
import json

# Load the `client` module directly from the repository to avoid import issues
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import importlib
client_module = importlib.import_module('client.client')

from types import SimpleNamespace

import pytest


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


class CM:
    def __init__(self, container):
        self._ws = FakeWS()
        self._container = container

    async def __aenter__(self):
        self._container["ws"] = self._ws
        return self._ws

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(client_module, "_plex_clients_cache", None, raising=False)
    monkeypatch.setattr(client_module, "_plex_server", None, raising=False)
    monkeypatch.setattr(client_module, "_current_playing_client", None, raising=False)
    monkeypatch.setattr(client_module, "_current_playing_filekey", None, raising=False)
    monkeypatch.setattr(client_module, "_current_playing_filename", None, raising=False)


def test_run_client_sends_registration(monkeypatch):
    # Prepare settings with two authorized client names
    fake_settings = SimpleNamespace(
        server_url="http://orchestrator",
        username="tester",
        passkey="secret",
        plex_url="http://plex",
        plex_token="token",
        authorized_clients="Local,Other",
    )

    client_module.settings = fake_settings

    # Return fake clients for the authorized names
    client_a = SimpleNamespace(title="Local", machineIdentifier="m1")
    client_b = SimpleNamespace(title="Other", machineIdentifier="m2")
    monkeypatch.setattr(client_module, "fetch_plex_clients", lambda: None)
    monkeypatch.setattr(client_module, "get_plex_client_by_name", lambda n: client_a if n == "Local" else client_b)

    container = {}
    # Patch websockets.connect to return our context manager (accept any args/kwargs)
    monkeypatch.setattr(client_module.websockets, "connect", lambda *args, **kwargs: CM(container))

    # Short-circuit the loops so run_client completes quickly
    async def quick_recv(ws):
        return

    async def quick_status(ws):
        return

    monkeypatch.setattr(client_module, "receive_loop", quick_recv)
    monkeypatch.setattr(client_module, "status_loop", quick_status)

    # Run client and ensure registration was sent
    asyncio.run(client_module.run_client())

    assert "ws" in container, "websocket context should have been entered"
    sent = container["ws"].sent
    assert sent, "expected registration to be sent"
    payload = json.loads(sent[0])
    assert payload["type"] == "register"
    # authorized_clients should include both entries with title and id
    ids = {c["id"] for c in payload.get("authorized_clients", [])}
    assert "m1" in ids and "m2" in ids


def test_status_loop_clears_on_mismatch(monkeypatch):
    # Prepare ws whose send will raise CancelledError after recording
    class WS(FakeWS):
        async def send(self, data):
            self.sent.append(data)
            raise asyncio.CancelledError()

    ws = WS()

    # Set global playing info so status_loop will attempt to send
    monkeypatch.setattr(client_module, "_current_playing_client", object(), raising=False)
    monkeypatch.setattr(client_module, "_current_playing_filekey", "expected", raising=False)
    monkeypatch.setattr(client_module, "_current_playing_filename", "file.mp4", raising=False)

    # Plex shows a different playing key -> mismatch should clear globals
    monkeypatch.setattr(client_module, "get_plex_server", lambda: object())
    monkeypatch.setattr(client_module, "get_current_playing_info", lambda ps, tc: ("other", 100))

    # Patch sleep so the loop will exit after one extra sleep by raising CancelledError
    calls = {"n": 0}

    async def fake_sleep(n):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError()
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    # Run status_loop; it should catch CancelledError and return
    asyncio.run(client_module.status_loop(ws))

    # Globals should be cleared due to mismatch
    assert client_module._current_playing_client is None
    assert client_module._current_playing_filekey is None
    assert client_module._current_playing_filename is None
