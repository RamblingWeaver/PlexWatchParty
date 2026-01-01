import pytest
import asyncio

from server.websocket_manager import WebSocketManager
from shared.models import WSCommand, Device, CommandType


class StubConn:
    def __init__(self):
        self.authorized_clients = []
        self.sent = []

    async def send_command(self, cmd: WSCommand):
        self.sent.append(cmd)


@pytest.mark.asyncio
async def test_get_authorized_and_broadcast():
    mgr = WebSocketManager()
    a = StubConn()
    a.authorized_clients = [Device(title="Local", id="m1")]
    b = StubConn()
    await mgr.add("alice", a)
    await mgr.add("bob", b)

    clients = await mgr.get_authorized_clients("alice")
    assert len(clients) == 1

    # broadcast single
    cmd = WSCommand(type=CommandType.PLAY, offset=0, filename="f.mp4", device=Device(title="Local", id="m1"))
    await mgr.broadcast("alice", cmd)
    assert a.sent and a.sent[-1].type == CommandType.PLAY


@pytest.mark.asyncio
async def test_broadcast_many_handles_missing_and_exceptions(monkeypatch):
    mgr = WebSocketManager()

    class BadConn:
        async def send_command(self, cmd):
            raise RuntimeError("boom")

    good = StubConn()
    await mgr.add("good", good)
    await mgr.add("bad", BadConn())

    cmd_map = {
        "good": WSCommand(type=CommandType.PAUSE, offset=None, filename="x", device=Device(title="Local", id="m1")),
        "bad": WSCommand(type=CommandType.PAUSE, offset=None, filename="x", device=Device(title="Local", id="m1")),
        "missing": WSCommand(type=CommandType.PAUSE, offset=None, filename="x", device=Device(title="Local", id="m1")),
    }

    # should not raise despite the bad sender
    await mgr.broadcast_many(cmd_map)
    assert good.sent and good.sent[-1].type == CommandType.PAUSE