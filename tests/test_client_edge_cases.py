import sys
import os
import asyncio
import json

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

import pytest


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)
        raise asyncio.CancelledError()


class AsyncIterator:
    def __init__(self, items):
        self._it = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(client_module, "_plex_clients_cache", None, raising=False)
    monkeypatch.setattr(client_module, "_plex_server", None, raising=False)
    monkeypatch.setattr(client_module, "_current_playing_client", None, raising=False)
    monkeypatch.setattr(client_module, "_current_playing_filekey", None, raising=False)
    monkeypatch.setattr(client_module, "_current_playing_filename", None, raising=False)


def test_handle_command_wrapped_cmd(monkeypatch):
    # command nested under 'cmd' key should be accepted
    dummy = type("D", (), {"play_calls": [], "playMedia": lambda self, m, *a, **k: self.play_calls.append((m, k)), "title": "Local", "machineIdentifier": "m1"})()
    monkeypatch.setattr(client_module, "get_plex_client_by_name", lambda x: dummy)
    async def fake_find(ps, filename):
        return type("M", (), {"key": "/lib/1"})()
    monkeypatch.setattr(client_module, "find_media_by_filename", fake_find)
    monkeypatch.setattr(client_module, "fetch_plex_clients", lambda: None)
    monkeypatch.setattr(client_module, "get_current_playing_key", lambda ps, tc: None)

    wrapped = {"cmd": {"type": "play", "device": {"title": "Local", "id": "m1"}, "filename": "f.mp4"}}
    asyncio.run(client_module.handle_command(wrapped))
    assert dummy.play_calls


def test_handle_command_no_device_clears_globals(monkeypatch):
    # if device not specified or not matched, globals must be cleared
    monkeypatch.setattr(client_module, "_current_playing_filename", "x.mp4", raising=False)
    monkeypatch.setattr(client_module, "_current_playing_filekey", "/lib/2", raising=False)
    monkeypatch.setattr(client_module, "fetch_plex_clients", lambda: None)

    # device specified but not matched by discovery
    asyncio.run(client_module.handle_command({"type": "play", "device": {"title": "Other", "id": "no-match"}, "filename": "x.mp4"}))

    assert client_module._current_playing_filename is None
    assert client_module._current_playing_filekey is None


def test_handle_play_when_plex_unavailable(monkeypatch):
    # Ensure graceful no-op when Plex server unavailable
    dummy = type("D", (), {"play_calls": [], "playMedia": lambda self, m, *a, **k: self.play_calls.append((m, k)), "title": "Local", "machineIdentifier": "m1"})()
    monkeypatch.setattr(client_module, "get_plex_client_by_name", lambda x: dummy)
    monkeypatch.setattr(client_module, "fetch_plex_clients", lambda: None)
    monkeypatch.setattr(client_module, "get_plex_server", lambda: None)

    async def fake_find(ps, filename):
        # should not be called with ps=None but if called, return None
        return None

    monkeypatch.setattr(client_module, "find_media_by_filename", fake_find)

    asyncio.run(client_module.handle_command({"type": "play", "device": {"title": "Local", "id": "m1"}, "filename": "y.mp4"}))
    # no play attempted
    assert not dummy.play_calls


def test_receive_loop_handles_invalid_json(monkeypatch):
    # Create a ws that yields invalid JSON; handle_command should not be called
    msgs = ["not-json"]
    ws = AsyncIterator(msgs)
    called = {"handled": False}

    async def fake_handle(cmd):
        called["handled"] = True

    monkeypatch.setattr(client_module, "handle_command", fake_handle)

    # Should not raise
    asyncio.run(client_module.receive_loop(ws))
    assert not called["handled"]


def test_status_loop_sends_update_and_exits(monkeypatch):
    ws = FakeWS()
    # set module globals so status_loop will try to send
    dummy_client = object()
    monkeypatch.setattr(client_module, "_current_playing_client", dummy_client, raising=False)
    monkeypatch.setattr(client_module, "_current_playing_filekey", "fk", raising=False)
    monkeypatch.setattr(client_module, "_current_playing_filename", "f.mp4", raising=False)

    # get_plex_server should be present; get_current_playing_info should report matching key and offset
    monkeypatch.setattr(client_module, "get_plex_server", lambda: object())
    monkeypatch.setattr(client_module, "get_current_playing_info", lambda ps, tc: ("fk", 12345))

    # Patch sleep to avoid delays
    async def fake_sleep(n):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    # status_loop should return cleanly after ws.send triggers CancelledError internally
    asyncio.run(client_module.status_loop(ws))

    # verify that a status_update payload was sent
    assert ws.sent, "expected status update to be sent"
    payload = json.loads(ws.sent[0])
    assert payload["type"] == "status_update"
