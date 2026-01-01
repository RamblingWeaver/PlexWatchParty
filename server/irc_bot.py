import asyncio
import logging
from typing import Optional
import pydle

from .config import settings

logger = logging.getLogger(__name__)


class IRCClient(pydle.Client):
    def __init__(self, orchestrator, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.orchestrator = orchestrator
        from .websocket_manager import WebSocketManager
        self.ws_manager: WebSocketManager = orchestrator.ws

    async def on_connect(self):
        if settings.irc_channel:
            await self.join(settings.irc_channel)
            logger.info("Joined IRC channel %s", settings.irc_channel)

    async def on_message(self, target, source, message):
        # message handling: !join, !leave, !pause, !resume
        txt = message.strip()
        if txt.startswith("!join"):
            # source is nick; mapping to orchestrator join requires more upstream mapping (username)
            await self.message(target, f"{source}: join request received")
        elif txt.startswith("!devices"):
            # list devices the client registered
            username = source
            clients = await self.ws_manager.get_authorized_clients(username)
            if clients:
                out = []
                for c in clients:
                    # c is a Device model
                    title = getattr(c, "title", "<unknown>")
                    mid = getattr(c, "id", None)
                    if mid:
                        out.append(f"{title} ({mid})")
                    else:
                        out.append(f"{title}")
                await self.message(target, f"{source}: authorized devices: {', '.join(out)}")
            else:
                await self.message(target, f"{source}: no registered devices found")
        elif txt.startswith("!use"):
            # usage: !use <device_name> <session_id>
            parts = txt.split()
            if len(parts) < 3:
                await self.message(target, f"{source}: usage: !use <device_name> <session_id>")
                return
            device = parts[1]
            session_id = parts[2]
            username = source
            # find device in authorized list (match title or id)
            clients = await self.ws_manager.get_authorized_clients(username)
            chosen = None
            for c in clients:
                # c is Device model
                if getattr(c, "title", None) == device or getattr(c, "id", None) == device:
                    chosen = getattr(c, "id", None) or getattr(c, "title", None)
                    break
            if not chosen:
                await self.message(target, f"{source}: device not found or not authorized: {device}")
                return
            try:
                await self.orchestrator.set_participant_client(session_id, username, chosen)
                await self.message(target, f"{source}: set device to {device} for session {session_id}")
            except KeyError as e:
                await self.message(target, f"{source}: error: {str(e)}")
        elif txt.startswith("!pause"):
            # Optionally: orchestrator.pause
            await self.message(target, "Pause requested")


async def start_irc(orchestrator):
    if not settings.irc_server or not settings.irc_channel:
        logging.info("IRC not configured; skipping IRC startup")
        return None

    client = IRCClient(orchestrator, settings.irc_nick)
    loop = asyncio.get_event_loop()
    task = loop.create_task(client.connect(settings.irc_server, settings.irc_port, tls=False))
    return client
