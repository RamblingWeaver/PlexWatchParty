import asyncio
from datetime import timedelta
import pytest

from server.orchestrator import Orchestrator, now
from shared.models import Device, CommandType, WSCommand


class DummyWS:
    def __init__(self):
        self.broadcast_calls = []
        self.broadcast_many_calls = []

    async def get_authorized_clients(self, username):
        return [Device(title="Local", id="m1")]

    async def broadcast(self, username, cmd):
        self.broadcast_calls.append((username, cmd))

    async def broadcast_many(self, cmd_map):
        self.broadcast_many_calls.append(cmd_map)


@pytest.mark.asyncio
async def test_add_participant_pre_and_post_start():
    ws = DummyWS()
    orch = Orchestrator(ws)

    # create a session (scheduled start time in the past so manual start is allowed)
    scheduled = now() - timedelta(seconds=1)
    session = await orch.create_session("movie.mp4", duration_ms=60_000, scheduled_start_time=scheduled)

    # add an early participant (session not started yet)
    await orch.add_participant(session.session_id, "alice", "Local")
    assert "alice" in session.participants
    assert "alice" in session.prequeue
    assert not ws.broadcast_calls
    assert not ws.broadcast_many_calls

    # start the session; prequeued user should receive a PLAY via broadcast_many
    await orch.start_session(session.session_id)
    assert ws.broadcast_many_calls, "expected broadcast_many on start"
    # ensure PLAY command for alice
    sent_map = ws.broadcast_many_calls[-1]
    assert "alice" in sent_map
    cmd = sent_map["alice"]
    assert cmd.type == CommandType.PLAY
    assert cmd.offset == 0

    # add a late joining participant: should get immediate PLAY via broadcast
    await orch.add_participant(session.session_id, "bob", "Local")
    assert ws.broadcast_calls, "expected single broadcast for late join"
    u, c = ws.broadcast_calls[-1]
    assert u == "bob"
    assert c.type == CommandType.PLAY


@pytest.mark.asyncio
async def test_pause_and_resume_flow():
    ws = DummyWS()
    orch = Orchestrator(ws)
    scheduled = now() - timedelta(seconds=1)
    session = await orch.create_session("movie2.mp4", duration_ms=3_600_000, scheduled_start_time=scheduled)

    # add participants and start immediately
    await orch.add_participant(session.session_id, "alice", "Local")
    await orch.start_session(session.session_id)

    # pause the session
    await orch.pause_session(session.session_id)
    assert ws.broadcast_many_calls, "expected PAUSE broadcast_many"
    pause_map = ws.broadcast_many_calls[-1]
    for cmd in pause_map.values():
        assert cmd.type == CommandType.PAUSE

    # resume the session
    await orch.resume_session(session.session_id)
    resume_map = ws.broadcast_many_calls[-1]
    for cmd in resume_map.values():
        assert cmd.type == CommandType.PLAY


@pytest.mark.asyncio
async def test_stop_background_tasks_cancels_tasks():
    ws = DummyWS()
    orch = Orchestrator(ws)
    scheduled = now() - timedelta(seconds=1)
    session = await orch.create_session("t.mp4", duration_ms=1000, scheduled_start_time=scheduled)

    # attach dummy tasks to session
    async def sleeper():
        await asyncio.sleep(10)

    t1 = asyncio.create_task(sleeper())
    t2 = asyncio.create_task(sleeper())
    async with orch._lock:
        s = orch._sessions.get(session.session_id)
        s.start_task = t1
        s.pause_task = t2

    await orch.stop_background_tasks()
    async with orch._lock:
        s2 = orch._sessions.get(session.session_id)
        assert getattr(s2, "start_task") is None
        assert getattr(s2, "pause_task") is None


@pytest.mark.asyncio
async def test_handle_client_status_update_triggers_seek():
    ws = DummyWS()
    orch = Orchestrator(ws)
    # create and start session such that server offset diverges from client's report
    scheduled = now() - timedelta(seconds=5)
    session = await orch.create_session("seekfile.mp4", duration_ms=600_000, scheduled_start_time=scheduled)
    await orch.add_participant(session.session_id, "alice", "Local")
    await orch.start_session(session.session_id)

    # simulate the session started earlier so server offset is non-zero
    session.start_time = now() - timedelta(seconds=10)

    # call handler with offset 0 and small threshold so SEEK will be sent
    await orch.handle_client_status_update(username="alice", filename="seekfile.mp4", offset=0, receive_time=now(), threshold_ms=1)
    assert ws.broadcast_calls, "expected SEEK broadcast to be sent"
    u, cmd = ws.broadcast_calls[-1]
    assert u == "alice"
    assert cmd.type == CommandType.SEEK
