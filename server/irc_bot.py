"""IRC bridge client.

Provides a small IRC client that accepts textual commands from an IRC
channel and maps them to orchestrator actions (create session, join,
leave, list, etc.). Handlers are intentionally lightweight — network
operations are awaited outside any shared orchestrator locks.
"""

import asyncio
import logging
from typing import Optional

import pydle
from datetime import datetime, timezone

from . import config

logger = logging.getLogger(__name__)


class IRCClient(pydle.Client):
    """Lightweight IRC client exposing session commands.

    Commands supported (via messages starting with "!"):
    - !devices: list authorized devices for the sender
    - !join <device_name> <session_id>: join a session using a device
    - !leave [session_id]: leave a session (or auto-discover the user's session)
    - !create <filename> <duration_minutes> [start_iso|now]: create session
    - !list: list active sessions
    """
    def __init__(self, orchestrator, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.orchestrator = orchestrator
        from .websocket_manager import WebSocketManager
        self.ws_manager: WebSocketManager = orchestrator.ws

    async def on_connect(self):
        """Join the configured IRC channel on connect.

        Called by pydle when the client establishes a connection.
        """
        settings = config.get_settings()
        if settings.irc_channel:
            await self.join(settings.irc_channel)
            logger.info("Joined IRC channel %s", settings.irc_channel)

    async def on_message(self, target, source, message):
        """Dispatch incoming IRC messages to command handlers.

        Only messages that begin with "!" are considered commands. The
        dispatcher is intentionally simple — each command handler is an
        `async` method that performs validation and communicates back to
        the IRC `target` (channel or user).
        """
        # Basic parsing: commands start with '!'
        txt = message.strip()
        if not txt.startswith("!"):
            return

        cmd = txt.split()[0][1:]
        try:
            if cmd == "devices":
                await self.handle_devices(target, source)
            elif cmd == "join":
                await self.handle_join(target, source, txt)
            elif cmd == "leave":
                await self.handle_leave(target, source, txt)
            elif cmd == "create":
                await self.handle_create(target, source, txt)
            elif cmd == "list":
                await self.handle_list(target)
        except Exception:
            # Catch errors at the top level so a misbehaving handler does
            # not crash the IRC client; log the stack and inform the user.
            logger.exception("Error handling IRC command %s from %s", cmd, source)
            await self.message(target, f"{source}: internal error handling command {cmd}")

    async def handle_devices(self, target, source):
        """List devices authorized for the `source` IRC user.

        Replies to the `target` (channel or user) with a short summary.
        """
        username = source
        clients = await self.ws_manager.get_authorized_clients(username)
        if clients:
            out = [getattr(c, "title") for c in clients]
            await self.message(target, f"{source}: authorized devices: {', '.join(out)}")
        else:
            await self.message(target, f"{source}: no registered devices found")

    async def handle_join(self, target, source, txt):
        """Allow a user to join a session with a chosen device.

        Device names may contain spaces — parsing treats the last token as
        the `session_id` and everything between `!join` and the last token
        as the device name.
        """
        # usage validation
        parts = txt.split()
        if len(parts) < 3:
            await self.message(target, f"{source}: usage: !join <device_name> <session_id>")
            return

        session_id = parts[-1]
        device = " ".join(parts[1:-1])
        username = source

        # Resolve authorized devices for user and match by title.
        clients = await self.ws_manager.get_authorized_clients(username)
        client = None
        for c in clients:
            if getattr(c, "title", None) == device:
                client = c
                break

        if not client:
            await self.message(target, f"{source}: device not found or not authorized: {device}")
            return

        try:
            await self.orchestrator.add_participant(session_id, username, client.title)
            await self.message(target, f"{source}: set device to {device} for session {session_id}")
        except KeyError as e:
            await self.message(target, f"{source}: error: {str(e)}")
        except ValueError as e:
            await self.message(
                target,
                f"{source}: {str(e)} — use !leave to leave your current session before joining another",
            )

    async def handle_leave(self, target, source, txt):
        """Remove a user from a session.

        If `session_id` is omitted, attempt to discover the session the
        user is currently in by scanning active sessions under the
        orchestrator lock.
        """
        parts = txt.split()
        username = source

        if len(parts) >= 2:
            session_id = parts[1]
        else:
            # Attempt to locate user's session under the orchestrator lock.
            session_id = None
            try:
                async with self.orchestrator._lock:
                    for sid, sess in self.orchestrator._sessions.items():
                        if username in getattr(sess, "participants", {}):
                            session_id = sid
                            break
            except Exception:
                logger.exception("Failed to resolve user's session for leave via IRC")
                await self.message(target, f"{source}: error determining current session")
                return

        if not session_id:
            await self.message(target, f"{source}: you are not in any session")
            return

        try:
            await self.orchestrator.remove_participant(session_id, username)
            await self.message(target, f"{source}: left session {session_id}")
        except KeyError:
            await self.message(target, f"{source}: session not found: {session_id}")
        except ValueError:
            await self.message(target, f"{source}: you are not a participant of session {session_id}")
        except Exception as e:
            logger.exception("Error removing participant %s from session %s", username, session_id)
            await self.message(target, f"{source}: error leaving session: {str(e)}")

    async def handle_create(self, target, source, txt):
        """Create a new session.

        Usage: `!create <filename> <duration_minutes> [start_iso|now]`.
        The start time may be specified as ISO8601 or the word `now`.
        """
        parts = txt.split()
        if len(parts) < 3:
            await self.message(target, f"{source}: usage: !create <filename> <duration_minutes> [start_iso|now]")
            return

        filename = parts[1]
        try:
            duration_min = int(parts[2])
        except Exception:
            await self.message(target, f"{source}: invalid duration (minutes): {parts[2]}")
            return

        duration_ms = int(duration_min) * 60 * 1000

        # Parse optional start time
        if len(parts) >= 4:
            start_raw = parts[3]
            try:
                if start_raw.lower() == "now":
                    scheduled = datetime.now(timezone.utc)
                else:
                    scheduled = datetime.fromisoformat(start_raw)
                    if scheduled.tzinfo is None:
                        scheduled = scheduled.replace(tzinfo=timezone.utc)
                    else:
                        scheduled = scheduled.astimezone(timezone.utc)
            except Exception:
                await self.message(target, f"{source}: invalid start time; use ISO8601 or 'now'")
                return
        else:
            scheduled = datetime.now(timezone.utc)

        try:
            session = await self.orchestrator.create_session(filename, duration_ms, scheduled)
            short_fn = (filename[:20] + "...") if len(filename) > 23 else filename
            await self.message(
                target,
                f"{source}: created session {short_fn} [{getattr(session, 'session_id', '<unknown>')}] scheduled {getattr(session, 'scheduled_start_time', scheduled).isoformat()}",
            )
        except Exception as e:
            await self.message(target, f"{source}: error creating session: {str(e)}")

    async def handle_list(self, target):
        """List active sessions (compact representation).

        Results are chunked to avoid overly long IRC messages.
        """
        try:
            async with self.orchestrator._lock:
                items = [
                    (sid, getattr(sess, "filename", "<unknown>"))
                    for sid, sess in self.orchestrator._sessions.items()
                ]
        except Exception:
            logger.exception("Failed to list sessions via IRC")
            await self.message(target, "Error listing sessions")
            return

        if not items:
            await self.message(target, "No active sessions")
            return

        # Truncate filenames for display and chunk results.
        lines = [f"{fn[:20]}... [{sid}]" for sid, fn in items]
        CHUNK = 5
        for i in range(0, len(lines), CHUNK):
            chunk = lines[i : i + CHUNK]
            await self.message(target, f"Sessions: {', '.join(chunk)}")


async def start_irc(orchestrator) -> Optional[IRCClient]:
    """Create and connect the IRC client if IRC is configured.

    Returns the connected `IRCClient` instance or `None` if IRC is not
    configured.
    """
    settings = config.get_settings()
    if not settings.irc_server or not settings.irc_channel:
        logger.info("IRC not configured; skipping IRC startup")
        return None

    client = IRCClient(orchestrator, settings.irc_nick)
    loop = asyncio.get_event_loop()
    loop.create_task(client.connect(settings.irc_server, settings.irc_port, tls=False))
    return client
