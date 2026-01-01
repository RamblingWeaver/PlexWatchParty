import asyncio
from datetime import timedelta
import pytest

from server.orchestrator import Orchestrator, now
from server.models import PauseInterval
from shared.models import Device, CommandType


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
async def test_schedule_next_pause_creates_task_when_not_paused():
    ws = DummyWS()
    orch = Orchestrator(ws)
    scheduled = now() - timedelta(seconds=10)
    session = await orch.create_session("f.mp4", duration_ms=24 * 3600 * 1000, scheduled_start_time=scheduled)

    # ensure session started so elapsed_ms > 0
    session.start_time = now() - timedelta(seconds=5)

    # no pauses currently
    assert not session.pause_intervals

    await orch._schedule_next_pause(session.session_id)

    async with orch._lock:
        s = orch._sessions.get(session.session_id)
        assert getattr(s, "pause_task") is not None


@pytest.mark.asyncio
async def test_schedule_next_pause_skips_when_paused():
    ws = DummyWS()
    orch = Orchestrator(ws)
    scheduled = now() - timedelta(seconds=10)
    session = await orch.create_session("g.mp4", duration_ms=24 * 3600 * 1000, scheduled_start_time=scheduled)
    session.start_time = now() - timedelta(seconds=5)

    # simulate currently paused (last interval end is None)
    session.pause_intervals.append(PauseInterval(start=now(), end=None))

    await orch._schedule_next_pause(session.session_id)
    async with orch._lock:
        s = orch._sessions.get(session.session_id)
        assert getattr(s, "pause_task") is None


@pytest.mark.asyncio
async def test_pause_session_schedules_auto_resume_and_resumes():
    ws = DummyWS()
    orch = Orchestrator(ws)
    # shorten pause_seconds for test so auto-resume happens quickly
    orch._pause_seconds = 0.05

    scheduled = now() - timedelta(seconds=10)
    session = await orch.create_session("h.mp4", duration_ms=600_000, scheduled_start_time=scheduled)
    # add a participant so broadcast_many has recipients
    await orch.add_participant(session.session_id, "alice", "Local")
    # mark session started
    session.start_time = now() - timedelta(seconds=1)

    # perform pause; this should create resume_task
    await orch.pause_session(session.session_id)
    async with orch._lock:
        s = orch._sessions.get(session.session_id)
        assert getattr(s, "resume_task") is not None
        # pause interval should have been appended with end None
        assert s.pause_intervals and s.pause_intervals[-1].end is None

    # wait longer than _pause_seconds to allow auto-resume to run
    await asyncio.sleep(0.12)

    # after auto-resume, resume_task might be None or completed; check that a PLAY was broadcast
    assert ws.broadcast_many_calls, "expected PLAY broadcast after auto-resume"
    last = ws.broadcast_many_calls[-1]
    for cmd in last.values():
        assert cmd.type == CommandType.PLAY

    # ensure pause interval was ended (resume sets last.end)
    async with orch._lock:
        s2 = orch._sessions.get(session.session_id)
        assert s2.pause_intervals and s2.pause_intervals[-1].end is not None
