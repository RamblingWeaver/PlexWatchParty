import asyncio
import pytest

from server.websocket_manager import Connection, WebSocketManager
from shared.models import WSCommand, Device, CommandType


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, obj):
        self.sent.append(obj)


@pytest.mark.asyncio
async def test_connection_set_authorized_and_send_command():
    ws = FakeWebSocket()
    conn = Connection(ws, "alice")

    # mixed valid and invalid client entries
    await conn.set_authorized_clients([{"title": "Local", "id": "m1"}, {"title": "Bad"}, 123])
    assert len(conn.authorized_clients) == 1
    d = conn.authorized_clients[0]
    assert d.title == "Local"

    # send a command and ensure websocket received the model
    cmd = WSCommand(type=CommandType.PLAY, offset=0, filename="f.mp4", device=Device(title="Local", id="m1"))
    await conn.send_command(cmd)
    assert ws.sent and "cmd" in ws.sent[-1]


@pytest.mark.asyncio
async def test_websocket_manager_add_remove_list_get():
    mgr = WebSocketManager()
    ws1 = FakeWebSocket()
    c1 = Connection(ws1, "u1")
    await mgr.add("u1", c1)
    users = await mgr.list_users()
    assert "u1" in users
    got = await mgr.get("u1")
    assert got is c1
    await mgr.remove("u1")
    users2 = await mgr.list_users()
    assert "u1" not in users2
