import asyncio
from datetime import timedelta
import pytest

from server.orchestrator import Orchestrator, now
from server.models import PauseInterval, Session, Participant
from shared.models import Device, CommandType


class DummyWS:
    def __init__(self):
        self.broadcast_calls = []
        self.broadcast_many_calls = []

    async def get_authorized_clients(self, username):
        return [Device(title="Other", id="m2")]

    async def broadcast(self, username, cmd):
        self.broadcast_calls.append((username, cmd))

    async def broadcast_many(self, cmd_map):
        self.broadcast_many_calls.append(cmd_map)


@pytest.mark.asyncio
async def test_create_session_and_start_future_raises():
    ws = DummyWS()
    orch = Orchestrator(ws)
    # scheduled in future
    scheduled = now() + timedelta(seconds=60)
    session = await orch.create_session("future.mp4", duration_ms=10000, scheduled_start_time=scheduled)

    # explicit start should raise because scheduled start_time is in future
    with pytest.raises(ValueError):
        await orch.start_session(session.session_id)


@pytest.mark.asyncio
async def test_add_participant_not_authorized_raises():
    ws = DummyWS()
    orch = Orchestrator(ws)
    scheduled = now()
    session = await orch.create_session("file.mp4", duration_ms=1000, scheduled_start_time=scheduled)

    # get_authorized_clients returns device with title 'Other', so requesting 'Local' should fail
    with pytest.raises(ValueError):
        await orch.add_participant(session.session_id, "alice", "Local")


@pytest.mark.asyncio
async def test_start_session_with_prequeue_but_missing_participants_sends_nothing():
    ws = DummyWS()
    orch = Orchestrator(ws)
    scheduled = now() - timedelta(seconds=1)
    session = await orch.create_session("nopart.mp4", duration_ms=10000, scheduled_start_time=scheduled)

    # add username to prequeue but do not add participant object
    async with orch._lock:
        s = orch._sessions.get(session.session_id)
        s.prequeue.append("ghost")

    # starting should not error but also not send broadcasts
    await orch.start_session(session.session_id)
    assert not ws.broadcast_many_calls


@pytest.mark.asyncio
async def test_pause_session_no_participants_no_broadcast():
    ws = DummyWS()
    orch = Orchestrator(ws)
    scheduled = now() - timedelta(seconds=1)
    session = await orch.create_session("nope.mp4", duration_ms=10000, scheduled_start_time=scheduled)

    # pause without participants should not broadcast
    await orch.pause_session(session.session_id)
    assert ws.broadcast_many_calls == []


@pytest.mark.asyncio
async def test_resume_session_no_pause_intervals_no_broadcast():
    ws = DummyWS()
    orch = Orchestrator(ws)
    scheduled = now() - timedelta(seconds=1)
    session = await orch.create_session("r.mp4", duration_ms=10000, scheduled_start_time=scheduled)

    # resume when nothing paused should return without broadcast
    await orch.resume_session(session.session_id)
    assert ws.broadcast_many_calls == []


@pytest.mark.asyncio
async def test_calculate_offset_with_various_pauses():
    ws = DummyWS()
    orch = Orchestrator(ws)
    # create a session container (not through create_session since we need custom times)
    s = Session(session_id="s1", filename="x", duration_ms=10000, scheduled_start_time=now(), start_time=now())

    # elapsed 10 seconds
    s.start_time = now() - timedelta(seconds=10)
    # a past pause from t=2s to t=5s (3s paused)
    s.pause_intervals.append(PauseInterval(start=s.start_time + timedelta(seconds=2), end=s.start_time + timedelta(seconds=5)))
    # ongoing pause starting at t=7s (3s so far)
    s.pause_intervals.append(PauseInterval(start=s.start_time + timedelta(seconds=7), end=None))

    # calculate offset
    offset = orch._calculate_offset(s, now())
    # elapsed 10s == 10000ms; paused = 3s + ~3s = ~6000ms; so offset approx 4000ms
    assert 3500 <= offset <= 4500


@pytest.mark.asyncio
async def test_handle_client_status_update_none_or_not_participant():
    ws = DummyWS()
    orch = Orchestrator(ws)
    scheduled = now() - timedelta(seconds=1)
    session = await orch.create_session("s.mp4", duration_ms=10000, scheduled_start_time=scheduled)

    # username None should be ignored
    await orch.handle_client_status_update(None, "s.mp4", 0, receive_time=now())
    assert not ws.broadcast_calls

    # non-participant username should be ignored
    await orch.handle_client_status_update("nobody", "s.mp4", 0, receive_time=now())
    assert not ws.broadcast_calls


@pytest.mark.asyncio
async def test_handle_client_status_update_within_threshold_no_seek():
    ws = DummyWS()
    orch = Orchestrator(ws)
    scheduled = now() - timedelta(seconds=2)
    session = await orch.create_session("t.mp4", duration_ms=10000, scheduled_start_time=scheduled)
    # ensure authorized clients include the device named 'Local'
    async def _auth(u):
        return [Device(title="Local", id="m1")]
    ws.get_authorized_clients = _auth
    orch.ws = ws
    await orch.add_participant(session.session_id, "alice", "Local")
    # start session and set start_time so offset is small
    async with orch._lock:
        s = orch._sessions.get(session.session_id)
        s.start_time = now() - timedelta(seconds=1)

    # report offset approximately equal to server offset
    server_offset = orch._calculate_offset(s, now())
    await orch.handle_client_status_update("alice", s.filename, server_offset, receive_time=now(), threshold_ms=5000)
    # no seek should be sent
    assert not ws.broadcast_calls
