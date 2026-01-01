"""API module: exposes WebSocket endpoint and passkey verification.

This module provides a FastAPI application with a WebSocket endpoint at
`/ws`. Clients must present `username` and `passkey` query parameters; the
`verify_passkey` helper validates credentials against an external service.

"""

from datetime import datetime, timezone
from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, status
import httpx
from .websocket_manager import WebSocketManager, Connection
from .orchestrator import Orchestrator
from .models import WSCommand, ClientStatus
from . import config

logger = logging.getLogger(__name__)

# singletons for now; in production these can be injected
ws_manager = WebSocketManager()
orchestrator = Orchestrator(ws_manager=ws_manager)

# load settings at module import so the FastAPI app can use them
settings = config.get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup: do nothing ---
    yield
    # --- shutdown ---
    await orchestrator.stop_background_tasks()


app = FastAPI(title=settings.app_name, lifespan=lifespan)


async def verify_passkey(username: str, passkey: str) -> None:
    """Verify credentials via the configured external validation URL.

    POSTs JSON {"username": <username>, "passkey": <passkey>} to
    `settings.passkey_validation_url` and expects a 200 response with JSON
    containing {"valid": true} on success.

    Raises:
        HTTPException: 500 if validation URL is not configured, or 401 for
            missing/invalid credentials.
    """
    # Validation endpoint is required
    if not settings.passkey_validation_url:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="passkey validation not configured")

    # Missing username is unauthorized
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="username required")

    # Missing passkey is unauthorized
    if not passkey:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="passkey required")

    payload = {"username": username, "passkey": passkey}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(settings.passkey_validation_url, json=payload)
        if resp.status_code == 200:
            try:
                resp_json = resp.json()
                if resp_json.get("valid"):
                    return
            except Exception:
                # If no JSON or unexpected body, treat as invalid
                pass
    except Exception:
        logger.exception("Passkey validation request failed")

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid passkey")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for clients.

    Clients must connect with `username` and `passkey` as query parameters.
    After successful verification the connection is accepted and the server
    listens for JSON messages of types `register` and `status_update`.
    """

    # If configured, require secure websocket (wss) connections. Check the
    # ASGI scope `scheme` and common proxy header `x-forwarded-proto` for
    # evidence the connection was secured by TLS. If enforcement is enabled
    # and we don't detect HTTPS/WSS, reject the connection.
    try:
        require_wss = bool(settings.require_wss)
    except Exception:
        require_wss = False

    if require_wss:
        # safe retrieval of scope values
        try:
            scope_scheme = websocket.scope.get('scheme') if getattr(websocket, 'scope', None) else None
        except Exception:
            scope_scheme = None
        xfp = websocket.headers.get('x-forwarded-proto') if getattr(websocket, 'headers', None) else None
        if not ((scope_scheme == 'wss') or (xfp and xfp.split(',')[0].strip().lower() == 'https')):
            try:
                await websocket.close(code=1008)
            except Exception:
                pass
            return

    # Expect authentication via headers to avoid leaking secrets in URLs.
    # Clients should send `Authorization: Bearer <passkey>` and
    # `X-Username: <username>` headers on the WebSocket handshake.
    username = None
    passkey = None
    try:
        # websocket.headers behaves like a dict-like object
        username = websocket.headers.get("x-username")
        auth_hdr = websocket.headers.get("authorization")
        if auth_hdr and isinstance(auth_hdr, str) and auth_hdr.lower().startswith("bearer "):
            passkey = auth_hdr.split(None, 1)[1]
    except Exception:
        # any header parsing error -> treat as missing
        username = None
        passkey = None

    # Require both username and passkey at connect time; refuse otherwise.
    if not username or not passkey:
        try:
            await websocket.close(code=1008)
        except Exception:
            pass
        return

    # Verify credentials before accepting the WebSocket. If verification
    # fails, close the connection immediately and do not accept.
    try:
        await verify_passkey(username, passkey)
    except HTTPException:
        try:
            await websocket.close(code=1008)
        except Exception:
            pass
        return

    # Only accept after successful verification
    await websocket.accept()

    conn = Connection(websocket=websocket, username=username)
    await ws_manager.add(username, conn)
    try:
        while True:
            data = await websocket.receive_json()
            # expected client messages: status_update, register
            logger.debug("Received from %s: %s", username, data)
            if isinstance(data, dict):
                if data.get("type") == "register":
                    clients = data.get("authorized_clients") or []
                    try:
                        await conn.set_authorized_clients(clients)
                        logger.info("Registered authorized clients for %s: %s", username, clients)
                    except Exception:
                        logger.exception("Failed to set authorized clients for %s", username)
                elif data.get("type") == "status_update":
                    # capture the receive time once for both orchestration and storage
                    receive_time = datetime.now(timezone.utc)
                    # client status updates include filename and current_offset
                    filename = data.get("filename")
                    raw_offset = data.get("current_offset") or 0
                    try:
                        # normalize to integer milliseconds
                        offset = int(float(raw_offset))
                    except Exception:
                        offset = 0
                    # Only persist status and forward if filename is present —
                    # ClientStatus requires a filename.
                    if filename:
                        adjusted = None
                        try:
                            # First, let the orchestrator process the status update
                            # before mutating connection state to avoid holding the
                            # connection lock while orchestration may perform work.
                            adjusted = await orchestrator.handle_client_status_update(
                                username=username, filename=filename, offset=offset, receive_time=receive_time
                            )
                        except Exception:
                            logger.exception("Failed to handle status update for %s", username)

                        try:
                            # persist the last-known client status on the connection
                            client_status = ClientStatus(
                                username=username,
                                filename=filename,
                                current_offset=adjusted if adjusted is not None else offset,
                                last_update_time=datetime.now(timezone.utc),
                            )
                            await conn.set_status(client_status)
                        except Exception:
                            logger.exception("Failed to set client status for %s", username)
    except WebSocketDisconnect:
        if username:
            await ws_manager.remove(username)