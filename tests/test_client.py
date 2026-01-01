import asyncio
import pytest

from client import client as client_module

# Shared dummy/test helpers
from tests.test_helpers import DummyPart, DummyMedia, DummyItem, DummyClientObj


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    # Ensure module-level caches are clean between tests
    monkeypatch.setattr(client_module, "_plex_clients_cache", None, raising=False)
    monkeypatch.setattr(client_module, "_plex_server", None, raising=False)
    monkeypatch.setattr(client_module, "_current_playing_client", None, raising=False)
    monkeypatch.setattr(client_module, "_current_playing_filekey", None, raising=False)
    monkeypatch.setattr(client_module, "_current_playing_filename", None, raising=False)


def test_find_media_by_filename_found(monkeypatch):
    # Prepare a fake plex server with sections and media
    part = DummyPart("/media/mov.mp4")
    media = DummyMedia("/library/metadata/1", [part])
    item = type("I", (), {"media": [media], "key": "/library/metadata/1"})()
    section = type("S", (), {"all": lambda self: [item], "type": "movie", "title": "Movies"})()

    class FakePlex:
        def __init__(self):
            self.library = type("L", (), {"sections": lambda self: [section]})()

    # call async helper directly with our FakePlex
    found = asyncio.run(client_module.find_media_by_filename(FakePlex(), "mov.mp4"))
    assert found is not None
    assert found.key == "/library/metadata/1"


def test_find_media_by_filename_not_found(monkeypatch):
    class FakePlex:
        def __init__(self):
            self.library = type("L", (), {"sections": lambda self: []})()

    # call async helper directly with our FakePlex
    assert asyncio.run(client_module.find_media_by_filename(FakePlex(), "noexist.mp4")) is None


def test_handle_play_by_key(monkeypatch):
    # Ensure playing by key finds the media and calls playMedia
    dummy = DummyClientObj()

    def fake_get_client_by_name(name):
        return dummy

    monkeypatch.setattr(client_module, "get_plex_client_by_name", fake_get_client_by_name)
    monkeypatch.setattr(client_module, "fetch_plex_clients", lambda: None)
    monkeypatch.setattr(client_module, "get_plex_server", lambda: None, raising=False)

    class FakePlex:
        def __init__(self):
            pass

    media = DummyMedia("/library/metadata/2", [DummyPart("/a.mp4")])

    async def fake_find(ps, filename):
        return media

    monkeypatch.setattr(client_module, "find_media_by_filename", fake_find)
    monkeypatch.setattr(client_module, "get_current_playing_key", lambda ps, tc: None)

    cmd = {"type": "play", "device": {"title": "Local", "id": "machine-1"}, "filename": "a.mp4", "filekey": "/library/metadata/2", "offset": 0}
    asyncio.run(client_module.handle_command(cmd))

    assert dummy.play_calls != []


def test_handle_play_no_media(monkeypatch):
    dummy = DummyClientObj()
    monkeypatch.setattr(client_module, "get_plex_client_by_name", lambda x: dummy)
    async def fake_find(ps, filename):
        return None

    monkeypatch.setattr(client_module, "find_media_by_filename", fake_find)
    monkeypatch.setattr(client_module, "fetch_plex_clients", lambda: None)

    cmd = {"type": "play", "device": {"title": "Local", "id": "machine-1"}, "filename": "missing.mp4"}
    # Should not raise
    asyncio.run(client_module.handle_command(cmd))
    assert not dummy.play_calls


def test_handle_pause_seek_stop(monkeypatch):
    dummy = DummyClientObj()
    monkeypatch.setattr(client_module, "get_plex_client_by_name", lambda x: dummy)

    # Prepare playing state so pause/seek/stop will apply
    monkeypatch.setattr(client_module, "_current_playing_filename", "a.mp4", raising=False)
    monkeypatch.setattr(client_module, "_current_playing_filekey", "/library/metadata/2", raising=False)
    monkeypatch.setattr(client_module, "get_current_playing_key", lambda ps, tc: "/library/metadata/2")
    monkeypatch.setattr(client_module, "fetch_plex_clients", lambda: None)

    # Pause
    asyncio.run(client_module.handle_command({"type": "pause", "device": {"title": "Local", "id": "machine-1"}, "filename": "a.mp4"}))
    assert dummy.pause_called

    # Seek
    asyncio.run(client_module.handle_command({"type": "seek", "device": {"title": "Local", "id": "machine-1"}, "offset": 4500, "filename": "a.mp4"}))
    assert 4500000 in dummy.seek_calls

    # Stop
    asyncio.run(client_module.handle_command({"type": "stop", "device": {"title": "Local", "id": "machine-1"}, "filename": "a.mp4"}))
    assert dummy.stop_called


def test_handle_invalid_type(monkeypatch):
    # Unknown command type should be ignored gracefully
    dummy = DummyClientObj()
    monkeypatch.setattr(client_module, "get_plex_client_by_name", lambda x: dummy)
    monkeypatch.setattr(client_module, "fetch_plex_clients", lambda: None)
    asyncio.run(client_module.handle_command({"type": "sync", "device": {"title": "Local", "id": "machine-1"}, "filename": "a.mp4"}))


def test_play_same_movie_already_playing(monkeypatch):
    dummy = DummyClientObj()
    monkeypatch.setattr(client_module, "get_plex_client_by_name", lambda x: dummy)
    # find returns this media
    media = DummyMedia("/library/metadata/10", [DummyPart("/x.mp4")])
    async def fake_find(ps, filename):
        return media

    monkeypatch.setattr(client_module, "find_media_by_filename", fake_find)
    # indicate the same file is already playing
    monkeypatch.setattr(client_module, "get_current_playing_key", lambda ps, tc: "/library/metadata/10")
    monkeypatch.setattr(client_module, "_current_playing_filename", "x.mp4", raising=False)
    monkeypatch.setattr(client_module, "_current_playing_filekey", "/library/metadata/10", raising=False)
    monkeypatch.setattr(client_module, "fetch_plex_clients", lambda: None)

    cmd = {"type": "play", "device": {"title": "Local", "id": "machine-1"}, "filename": "x.mp4", "offset": 0}
    asyncio.run(client_module.handle_command(cmd))

    assert dummy.play_called is True
    assert not dummy.play_calls


def test_play_different_movie_playing(monkeypatch):
    dummy = DummyClientObj()
    monkeypatch.setattr(client_module, "get_plex_client_by_name", lambda x: dummy)
    media = DummyMedia("/library/metadata/new", [DummyPart("/new.mp4")])
    async def fake_find(ps, filename):
        return media

    monkeypatch.setattr(client_module, "find_media_by_filename", fake_find)
    # different key indicates another movie is playing
    monkeypatch.setattr(client_module, "get_current_playing_key", lambda ps, tc: "/library/metadata/old")
    monkeypatch.setattr(client_module, "_current_playing_filename", "old.mp4", raising=False)
    monkeypatch.setattr(client_module, "_current_playing_filekey", "/library/metadata/old", raising=False)
    monkeypatch.setattr(client_module, "fetch_plex_clients", lambda: None)

    cmd = {"type": "play", "device": {"title": "Local", "id": "machine-1"}, "filename": "new.mp4", "offset": 1000}
    asyncio.run(client_module.handle_command(cmd))

    # should have stopped and then played the new media (playMedia recorded)
    assert dummy.stop_called is True
    assert dummy.play_calls, "expected playMedia to be called for new media"


def test_play_no_current_starts_play(monkeypatch):
    dummy = DummyClientObj()
    monkeypatch.setattr(client_module, "get_plex_client_by_name", lambda x: dummy)
    media = DummyMedia("/library/metadata/solo", [DummyPart("/solo.mp4")])
    async def fake_find(ps, filename):
        return media

    monkeypatch.setattr(client_module, "find_media_by_filename", fake_find)
    monkeypatch.setattr(client_module, "get_current_playing_key", lambda ps, tc: None)
    monkeypatch.setattr(client_module, "fetch_plex_clients", lambda: None)

    cmd = {"type": "play", "device": {"title": "Local", "id": "machine-1"}, "filename": "solo.mp4", "offset": 2000}
    asyncio.run(client_module.handle_command(cmd))

    assert dummy.play_calls, "expected playMedia to be called when no current media"


def test_pause_seek_stop_do_not_affect_other_files(monkeypatch):
    dummy = DummyClientObj()
    monkeypatch.setattr(client_module, "get_plex_client_by_name", lambda x: dummy)
    # Module thinks a different file is recorded as playing
    monkeypatch.setattr(client_module, "_current_playing_filename", "a.mp4", raising=False)
    monkeypatch.setattr(client_module, "_current_playing_filekey", "/library/metadata/2", raising=False)
    # But Plex reports a different current session key
    monkeypatch.setattr(client_module, "get_current_playing_key", lambda ps, tc: "/library/metadata/other")
    monkeypatch.setattr(client_module, "fetch_plex_clients", lambda: None)

    # Attempt to pause a different filename
    asyncio.run(client_module.handle_command({"type": "pause", "device": {"title": "Local", "id": "machine-1"}, "filename": "b.mp4"}))
    assert not dummy.pause_called
    # Globals should have been cleared because mismatch triggers clear
    assert getattr(client_module, "_current_playing_filename") is None
    assert getattr(client_module, "_current_playing_filekey") is None

    # Reset module playing state to mismatch again
    monkeypatch.setattr(client_module, "_current_playing_filename", "a.mp4", raising=False)
    monkeypatch.setattr(client_module, "_current_playing_filekey", "/library/metadata/2", raising=False)
    monkeypatch.setattr(client_module, "get_current_playing_key", lambda ps, tc: "/library/metadata/other")

    # Attempt seek on different file; no seek should be performed
    asyncio.run(client_module.handle_command({"type": "seek", "device": {"title": "Local", "id": "machine-1"}, "offset": 3000, "filename": "b.mp4"}))
    assert not dummy.seek_calls

    # Attempt stop on different file; no stop call
    asyncio.run(client_module.handle_command({"type": "stop", "device": {"title": "Local", "id": "machine-1"}, "filename": "b.mp4"}))
    assert not dummy.stop_called


def test_run_client_raises_when_settings_missing_env(monkeypatch):
    # Simulate failure to load settings by making ClientSettings raise
    monkeypatch.setattr(client_module, "settings", None, raising=False)
    def raise_on_init():
        raise RuntimeError("no settings available")
    monkeypatch.setattr(client_module.config, "ClientSettings", raise_on_init)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(client_module.run_client())
    assert "Failed to load client settings" in str(exc.value)


def test_run_client_validates_required_fields(monkeypatch):
    # Provide a settings-like object that's missing/empty required fields
    class FakeSettings:
        server_url = "http://orchestrator"
        username = "tester"
        passkey = ""  # empty should be treated as missing
        plex_url = "http://plex"
        plex_token = "   "  # whitespace only should be treated as missing
        authorized_clients = None

    monkeypatch.setattr(client_module, "settings", FakeSettings(), raising=False)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(client_module.run_client())
    msg = str(exc.value)
    assert "Missing required client settings" in msg
    assert "passkey" in msg
    assert "plex_token" in msg
