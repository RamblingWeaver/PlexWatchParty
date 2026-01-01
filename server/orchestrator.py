from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import Optional, List, Dict
import uuid
import logging
from .models import Session, Participant, PauseInterval, WSCommand, CommandType, Device
from .websocket_manager import WebSocketManager

logger = logging.getLogger(__name__)

# Concurrency notes:
# - All accesses and mutations to `self._sessions` must be performed while
#   holding `self._lock` to avoid races.
# - Network I/O and any awaits that may block should be done outside the lock.
# - Scheduled background tasks are stored on the `Session` object for
#   visibility/cancellation.


def now() -> datetime:
    return datetime.now(timezone.utc)


class Orchestrator:
    """In-memory orchestrator. All session state is kept in-process in Python structures.

    This simplifies deployments where persistent storage is not required.
    """

    def __init__(self, ws_manager: WebSocketManager):
        self.ws = ws_manager
        self._sessions: Dict[str, Session] = {}
        self._lock = asyncio.Lock()
        # fixed pause duration (seconds) — always 10 minutes
        self._pause_seconds = 10 * 60

    async def stop_background_tasks(self) -> None:
        """Cancel per-session background tasks (start/pause/resume).

        This acquires the orchestrator lock and cancels any Task objects
        attached to sessions. Cancellation is best-effort and tasks are not
        awaited here.
        """
        async with self._lock:
            for session in list(self._sessions.values()):
                for attr in ("start_task", "pause_task", "resume_task"):
                    t = getattr(session, attr, None)
                    if t is not None:
                        try:
                            t.cancel()
                        except Exception:
                            # best-effort cancel; ignore errors
                            pass
                    setattr(session, attr, None)

    async def create_session(self, filename: str, duration_ms: int, scheduled_start_time: datetime) -> Session:
        session = Session(session_id=str(uuid.uuid4()), filename=filename, duration_ms=int(duration_ms), scheduled_start_time=scheduled_start_time, start_time=None)
        logger.info("Creating session %s filename=%s scheduled_start=%s duration_ms=%s", session.session_id, filename, scheduled_start_time.isoformat(), duration_ms)
        # define the scheduled-start coroutine (does not depend on local session var)
        async def _run_start(session_id: str):
            async with self._lock:
                session = self._sessions.get(session_id)
                if not session:
                    return
                delay = (session.scheduled_start_time - now()).total_seconds()

            if delay > 0:
                await asyncio.sleep(delay)

            try:
                await self.start_session(session_id)
            except Exception:
                logger.exception("Scheduled start failed for session %s", session_id)

        # register session under lock, then create the background task outside
        async with self._lock:
            self._sessions[session.session_id] = session

        start_task = asyncio.create_task(_run_start(session.session_id))
        async with self._lock:
            s = self._sessions.get(session.session_id)
            if s is not None:
                s.start_task = start_task
                logger.info("Scheduled start task for session %s", session.session_id)

        return session

    async def _schedule_next_pause(self, session_id: str) -> None:
        """Compute and schedule the next automatic pause for a session.

        Calculates the next hour boundary where a pause should occur and
        schedules a background task to call `pause_session` at that time.
        """
        HOUR_MS = 60 * 60 * 1000
        THIRTY_MIN_MS = 30 * 60 * 1000

        # compute scheduling params under lock, then create the task outside
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return
            # don't schedule while paused
            if session.pause_intervals and session.pause_intervals[-1].end is None:
                return
            elapsed_ms = self._calculate_offset(session, now())
            next_k = elapsed_ms // HOUR_MS + 1
            next_target_ms = next_k * HOUR_MS
            if next_k == 1:
                should_schedule = True
            else:
                remaining_after = session.duration_ms - next_target_ms
                should_schedule = remaining_after > THIRTY_MIN_MS
            if not should_schedule:
                return
            to_target_sec = max(0.0, (next_target_ms - elapsed_ms) / 1000.0)

        async def _run_pause(session_id: str, delay: float):
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            try:
                await self.pause_session(session_id)
            except Exception:
                logger.exception("Scheduled pause failed for session %s", session_id)

        pause_task = asyncio.create_task(_run_pause(session_id, to_target_sec))
        async with self._lock:
            s = self._sessions.get(session_id)
            if s is not None:
                s.pause_task = pause_task

    async def add_participant(self, session_id: str, username: str, client_name: str) -> None:
        """Add a participant to a session and (optionally) send PLAY.

        Resolves the participant's `Device` from authorized clients, stores
        the `Participant` under the session, and, if the session has
        already started, sends a PLAY to the new participant to catch them up.
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("session not found")

        # resolve the device object from authorized clients (do not await while holding lock)
        device_obj = None
        
        clients = await self.ws.get_authorized_clients(username)
        for device in clients:
            if device.title == client_name:
                device_obj = device
                break

        if device_obj is None:
            raise ValueError("specified client not authorized or not found for user")

        logger.info("Resolved device for user=%s -> title=%s id=%s", username, getattr(device_obj, 'title', None), getattr(device_obj, 'id', None))

        participant = Participant(username=username, device=device_obj, join_time=now(), offset=0)

        # Determine whether to send an immediate PLAY. Avoid awaiting while holding the lock.
        cmd_to_send = None
        async with self._lock:
            # store participant on the session
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("session not found")
            session.participants[username] = participant
            logger.info("Added participant %s to session %s (device=%s)", username, session_id, getattr(participant.device, 'title', None))
            # if the session already started, send immediate PLAY to this participant to catch them up
            if session.start_time:
                offset = self._calculate_offset(session, now())
                cmd_to_send = WSCommand(type=CommandType.PLAY, offset=offset, filename=session.filename, device=participant.device)
            else:
                # add to prequeue so new participants will receive initial play when session starts
                if username not in session.prequeue:
                    session.prequeue.append(username)

        # perform network IO outside the lock
        if cmd_to_send:
            await self.ws.broadcast(username, cmd_to_send)

    async def start_session(self, session_id: str) -> None:
        """Start a scheduled session and send initial PLAY commands.

        Captures and clears the `prequeue` under the lock so initial PLAY
        commands are sent once to queued users. PLAY commands are sent with
        per-user `Device` information and are dispatched concurrently.
        """
        # set start_time and take ownership of the prequeue under the lock
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("session not found")
            if session.start_time:
                return
            # ensure scheduled start time has passed (compare in UTC)
            if session.scheduled_start_time > now():
                raise ValueError("session scheduled start_time is in the future")
            session.start_time = now()
            logger.info("Session %s start_time set to %s; prequeue=%s", session_id, session.start_time.isoformat(), session.prequeue)
            # capture and clear prequeue so we only send initial plays once
            prequeue_users = list(session.prequeue)
            session.prequeue = []
            # snapshot participant devices for the prequeue users to avoid races
            participant_devices = {u: p.device for u, p in session.participants.items() if u in prequeue_users}

        # send play to captured prequeue users. Send a PLAY command to each
        # user using their resolved device so clients can align appropriately.
        if participant_devices:
            logger.info("Session %s will broadcast PLAY to users: %s", session_id, list(participant_devices.keys()))
            for u, dev in participant_devices.items():
                logger.info(" - recipient=%s device=%s", u, dev.model_dump())
            # Build per-user PLAY commands (each includes the recipient's device)
            cmd_map: Dict[str, WSCommand] = {
                user: WSCommand(
                    type=CommandType.PLAY,
                    offset=0,
                    filename=session.filename,
                    device=dev,
                )
                for user, dev in participant_devices.items()
            }
            # send all commands concurrently to avoid per-user await delays
            await self.ws.broadcast_many(cmd_map)

        # schedule the next pause for this session (if applicable)
        try:
            await self._schedule_next_pause(session_id)
        except Exception:
            logger.exception("Failed to schedule next pause for %s", session_id)

    async def pause_session(self, session_id: str) -> None:
        """Pause the session and notify participants.

        Appends a PauseInterval and schedules an automatic resume after the
        fixed pause duration. Sends per-user PAUSE commands concurrently so
        each recipient receives their selected `Device` info.
        """
        # prepare pause: append interval and snapshot participants under lock
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("session not found")
            session.pause_intervals.append(PauseInterval(start=now(), end=None))
            participant_devices = {u: p.device for u, p in session.participants.items()}
            prev_resume = getattr(session, "resume_task", None)

        async def _auto_resume(session_id: str):
            try:
                await asyncio.sleep(float(self._pause_seconds))
                try:
                    await self.resume_session(session_id)
                except Exception:
                    logger.exception("Auto-resume failed for session %s", session_id)
            except asyncio.CancelledError:
                return

        resume_task = asyncio.create_task(_auto_resume(session_id))
        # assign resume_task under lock and cancel previous if present
        async with self._lock:
            s = self._sessions.get(session_id)
            if s is not None:
                if prev_resume is not None:
                    try:
                        prev_resume.cancel()
                    except Exception:
                        pass
                s.resume_task = resume_task

        # send a PAUSE command to all participants concurrently so the
        # server-determined pause happens simultaneously for everyone, and
        # include each participant's selected `Device` in their command.
        # build and send PAUSE commands from the snapshot
        if participant_devices:
            cmd_map: Dict[str, WSCommand] = {
                user: WSCommand(
                    type=CommandType.PAUSE,
                    offset=None,
                    filename=session.filename,
                    device=dev,
                )
                for user, dev in participant_devices.items()
            }
            await self.ws.broadcast_many(cmd_map)

    async def resume_session(self, session_id: str) -> None:
        """Resume a paused session and notify participants.

        Marks the latest PauseInterval as ended and broadcasts PLAY to all
        participants concurrently. The fixed pause length is enforced by the
        caller that initiates pause/resume flows.
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("session not found")
            if not session.pause_intervals:
                return
            last = session.pause_intervals[-1]
            if last.end is not None:
                # already resumed
                return
            last.end = now()
            # compute server offset here while under lock
            server_offset = self._calculate_offset(session, now())
            # snapshot participant devices
            participant_devices = {u: p.device for u, p in session.participants.items()}

        # send a PLAY (resume) to all participants concurrently, including
        # each participant's selected `Device` in their command and the
        # computed resume offset.
        if participant_devices:
            cmd_map: Dict[str, WSCommand] = {
                user: WSCommand(
                    type=CommandType.PLAY,
                    offset=server_offset,
                    filename=session.filename,
                    device=dev,
                )
                for user, dev in participant_devices.items()
            }
            await self.ws.broadcast_many(cmd_map)
        # after resuming, schedule the next pause if appropriate
        try:
            await self._schedule_next_pause(session_id)
        except Exception:
            logger.exception("Failed to schedule next pause after resume for %s", session_id)

    def _calculate_offset(self, session: Session, current_time: datetime) -> int:
        """Return the session offset in integer milliseconds."""
        if not session.start_time:
            return 0
        elapsed_ms = int((current_time - session.start_time).total_seconds() * 1000)
        paused_ms = 0
        for p in session.pause_intervals:
            if p.end:
                paused_ms += int((p.end - p.start).total_seconds() * 1000)
            else:
                paused_ms += int((current_time - p.start).total_seconds() * 1000)
        return max(0, elapsed_ms - paused_ms)

    async def list_sessions(self) -> List[str]:
        async with self._lock:
            return [s.filename for s in self._sessions.values()]

    async def handle_client_status_update(self, username: Optional[str], filename: str, offset: int, receive_time: datetime, threshold_ms: int = 5000):
        """Handle a status update reported by a client.

        If the client's reported `offset` differs from the server's computed
        session offset by more than `threshold_ms` milliseconds, send a SEEK command
        to that client to correct its playback position.
        """
        async with self._lock:
            sessions = list(self._sessions.values())

        if username is None:
            return

        adjusted_offset = None
        for session in sessions:
            if session.filename != filename:
                continue
            if username not in session.participants:
                continue

            # Adjust the reported offset by the time elapsed since the
            # status update was received so comparisons use a closer
            # approximation of the client's playback position.
            server_offset = self._calculate_offset(session, now())
            delta_ms = int((now() - receive_time).total_seconds() * 1000)
            adjusted_offset = offset + delta_ms
            try:
                if abs(int(adjusted_offset) - int(server_offset)) > int(threshold_ms):
                    participant = session.participants.get(username)
                    if participant is None:
                        continue
                    # participant.device is required and represents the selected client device
                    cmd = WSCommand(type=CommandType.SEEK, offset=server_offset, filename=session.filename, device=participant.device)
                    await self.ws.broadcast(username, cmd)
            except Exception:
                logger.exception("Error handling status update for %s", username)

        return adjusted_offset
