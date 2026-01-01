from typing import Dict, Optional, List
import asyncio
import logging
from fastapi import WebSocket
from .models import WSCommand, Device, ClientStatus

logger = logging.getLogger(__name__)


"""WebSocket manager and connection helpers.

This module provides `Connection` which wraps a single FastAPI `WebSocket`
and `WebSocketManager` which tracks active connections and provides helper
methods for sending `WSCommand` messages to clients.
"""


class Connection:
    """Represents a single websocket connection and its metadata.

    The `Connection` holds a per-connection lock so sends and status
    updates are serialized per-socket.
    """

    def __init__(self, websocket: WebSocket, username: str):
        self.websocket = websocket
        self.username = username
        # store authorized clients as a list of `Device` models
        self.authorized_clients: List[Device] = []
        # last known status update from this connection
        self.last_status: Optional[ClientStatus] = None
        self._lock = asyncio.Lock()

    async def send_command(self, cmd: WSCommand) -> None:
        """Send a `WSCommand` to this connection.

        This method serializes sends using a per-connection lock to avoid
        interleaving writes on the same websocket.
        """
        async with self._lock:
            payload = {"cmd": cmd.model_dump()}
            logger.info("Sending command to %s: %s", self.username, payload)
            await self.websocket.send_json(payload)

    async def set_status(self, status: ClientStatus) -> None:
        """Persist the most-recent status update for this connection."""
        async with self._lock:
            self.last_status = status

    async def set_authorized_clients(self, clients: List[object]) -> None:
        """Normalize and store authorized client `Device` entries.

        Only entries that include both `title` and `id` are accepted. Invalid
        or incomplete entries are skipped. This method acquires the per-connection
        lock to avoid races with concurrent readers.
        """
        if not clients:
            async with self._lock:
                self.authorized_clients = []
            return

        normalized: List[Device] = []
        for c in clients:
            if isinstance(c, Device):
                if getattr(c, "title", None) and getattr(c, "id", None):
                    normalized.append(c)
                else:
                    continue
            elif isinstance(c, dict):
                try:
                    d = dict(c)
                    title = d.get("title")
                    device_id = d.get("id")
                    if not title or not device_id:
                        continue
                    normalized.append(Device.model_validate(d))
                except Exception:
                    continue
            else:
                continue

        async with self._lock:
            self.authorized_clients = normalized


class WebSocketManager:
    """Track active `Connection` objects and provide send helpers."""

    def __init__(self):
        self._connections: Dict[str, Connection] = {}
        self._lock = asyncio.Lock()

    async def add(self, username: str, conn: Connection) -> None:
        """Register a new connection for `username`. Overwrites any existing connection."""
        async with self._lock:
            self._connections[username] = conn
            logger.info("Added websocket for %s", username)

    async def remove(self, username: str) -> None:
        """Remove the connection for `username` if present."""
        async with self._lock:
            self._connections.pop(username, None)
            logger.info("Removed websocket for %s", username)

    async def get(self, username: str) -> Optional[Connection]:
        """Return the `Connection` for `username` or `None` if not connected."""
        async with self._lock:
            return self._connections.get(username)

    async def get_authorized_clients(self, username: str) -> List[Device]:
        """Return the list of authorized `Device` entries for `username`.

        Returns an empty list if the user is not connected or has none.
        """
        async with self._lock:
            c = self._connections.get(username)
            if c:
                return list(c.authorized_clients)
            return []

    async def broadcast(self, username: str, cmd: WSCommand) -> None:
        """Send a single command to one user if connected.

        For multi-recipient sends, use `broadcast_many`.
        """
        conn = await self.get(username)
        if conn:
            logger.info("Broadcasting single command to %s", username)
            await conn.send_command(cmd)

    async def broadcast_many(self, cmd_map: Dict[str, WSCommand]) -> None:
        """Broadcast individual commands per user concurrently.

        `cmd_map` is a mapping from username -> WSCommand. This allows each
        recipient to receive a command that can include a user-specific
        `Device` object without awaiting sequential sends.
        """
        # Snapshot connections under the manager lock to avoid repeated
        # acquisitions of the lock while creating send tasks.
        conns: Dict[str, Connection] = {}
        async with self._lock:
            for u in cmd_map.keys():
                c = self._connections.get(u)
                if c:
                    conns[u] = c

        tasks = []
        recipients_sent: List[str] = []
        for u, cmd in cmd_map.items():
            conn = conns.get(u)
            if conn:
                logger.info("Scheduling send to %s: %s", u, cmd.model_dump())
                tasks.append(asyncio.create_task(conn.send_command(cmd)))
                recipients_sent.append(u)

        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)
        # Log any exceptions raised during sends so failures are visible
        for u, res in zip(recipients_sent, results):
            if isinstance(res, Exception):
                logger.error("Error sending command to %s", u, exc_info=res)

    async def list_users(self) -> List[str]:
        """Return the list of connected usernames."""
        async with self._lock:
            return list(self._connections.keys())
