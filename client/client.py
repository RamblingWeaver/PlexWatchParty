"""Lightweight watch-party client pairing implementation.

This client maintains a persistent WebSocket connection to the orchestration
server, sends periodic status updates, and receives playback commands.

It intentionally does not implement Plex control — hooks are provided where
`plexapi` actions should be called on the local machine to control local clients.
"""
import asyncio
import json
import logging
import websockets
from plexapi.server import PlexServer
from plexapi.client import PlexClient
from plexapi.library import LibrarySection
from plexapi.media import Media, MediaPart
from . import config
from shared.models import WSCommand, Device, CommandType

logger = logging.getLogger(__name__)

# Global settings instance (created at startup)
settings: config.ClientSettings | None = None

# Optional test hook: when set to a list of dicts this list will be used
# as the registration `authorized_clients` payload instead of discovering
# local Plex clients. Interactive test runner sets this to mock registration.
_test_startup_auth: list[dict] | None = None

# Module-level caches and playback state
_plex_clients_cache: list[PlexClient] | None = []
_plex_server: PlexServer | None = None
_plex_server_settings: tuple | None = None
_current_playing_client = None
_current_playing_filekey = None
_current_playing_filename = None


def clear_playing_globals():
    """Clear stored information about the currently playing media.

    This resets the in-memory state used for status updates and command
    validation.
    """
    global _current_playing_client, _current_playing_filekey, _current_playing_filename
    _current_playing_client = None
    _current_playing_filekey = None
    _current_playing_filename = None

def get_plex_server():
    """Return a cached `PlexServer` instance, creating it if needed.

    Returns None when `settings` is not yet configured (tests or mocked
    environments).
    """
    global _plex_server
    global _plex_server_settings
    if _plex_server is not None:
        return _plex_server
    if settings is None:
        return None

    current = (str(settings.plex_url), settings.plex_token)
    try:
        _plex_server = PlexServer(current[0], current[1])
    except Exception:
        logger.warning("Failed to connect to Plex server at %s", current[0])
        _plex_server = None
        _plex_server_settings = None
        return None

    _plex_server_settings = current
    logger.info("Connected to Plex server at %s", current[0])

    return _plex_server


def fetch_plex_clients():
    """Discover clients from the local Plex Media Server and cache them.

    Returns a list of `PlexClient` objects with `title` and
    `machineIdentifier` attributes. This is best-effort and will leave the
    cache empty if no Plex server is available or clients not found.
    """
    ps = get_plex_server()
    global _plex_clients_cache
    if ps is None:
        # If settings are present but Plex server is unreachable, this
        # likely indicates a misconfiguration on the client machine.
        if settings is not None:
            logger.warning("Plex server unavailable; cannot discover local clients")
        _plex_clients_cache = []
        return []

    clients: list[PlexClient] = ps.clients()

    # ensure clients proxy through the plex server so commands are routed properly
    for client in clients:
        client.proxyThroughServer()

    _plex_clients_cache = clients
    logger.info("Discovered %d Plex client(s)", len(clients))


def get_plex_client_by_name(name_or_id: str):
    """Return a cached Plex client matched by title or machineIdentifier.

    Matching is case-sensitive. Returns ``None`` if no match is found or the
    cache is empty.
    """
    if not name_or_id:
        return None
    if not _plex_clients_cache:
        return None
    for c in _plex_clients_cache:
        if c.title == name_or_id:
            return c
        if c.machineIdentifier == name_or_id:
            return c
    return None


async def handle_command(cmd: dict):
    """Validate and dispatch an incoming WebSocket command.

    The server sends commands either as a raw dict or nested under a
    ``cmd`` key; both formats are supported here.
    """
    payload = WSCommand.model_validate(cmd.get("cmd") or cmd)

    ctype = payload.type
    offset = payload.offset
    filename = payload.filename
    device = payload.device

    # Log unpacked device info for diagnostics
    if isinstance(device, Device):
        logger.info("Unpacked device: title=%s id=%s", device.title, device.id)
    else:
        logger.debug("No device present in command")

    # Log remaining command info
    logger.info("Received command: %s offset=%s filename=%s", ctype, offset, filename)

    # Get Plex server (may be None in tests or mocked environments)
    ps = get_plex_server()
    if ps is None and ctype in (
        CommandType.PLAY,
        CommandType.PAUSE,
        CommandType.SEEK,
        CommandType.STOP,
    ):
        logger.warning("Plex server unavailable while handling command '%s'", ctype)

    # Refresh available Plex clients (best-effort)
    fetch_plex_clients()

    # Server-driven selection: command includes the target client in `device`
    target_spec = None
    target_client = None
    if isinstance(device, Device):
        target_spec = device.id
    if target_spec:
        target_client = get_plex_client_by_name(target_spec)
    if target_client is None:
        # Server must specify the target client; do not assume a local default
        clear_playing_globals()
        return

    # Execute commands using dedicated handlers
    if ctype == CommandType.PLAY:
        await _handle_play(target_client, filename, offset, ps)
    elif ctype == CommandType.PAUSE:
        await _handle_pause(target_client, filename, ps)
    elif ctype == CommandType.SEEK:
        await _handle_seek(target_client, filename, offset, ps)
    elif ctype == CommandType.STOP:
        await _handle_stop(target_client, filename, ps)
    else:
        logger.warning("Unknown command type: %s", ctype)


async def find_media_by_filename(ps: PlexServer, filename: str):
    """Search the Plex library for an item whose media part filename ends
    with the provided `filename`.

    Returns the matching item or ``None`` if not found.
    """
    logger.debug("Searching Plex library for filename: %s", filename)
    if ps is None:
        logger.debug("No Plex server available for media lookup")
        return None

    # Loop through all library sections
    for s in ps.library.sections():
        section: LibrarySection = s

        # Only consider video sections
        if section.type not in ("movie", "show", "episode"):
            continue

        # Iterate through all items in the section
        for item in section.all():
            media_list: list[Media] = item.media
            for media in media_list:
                parts_list: list[MediaPart] = media.parts
                for part in parts_list:
                    if part.file.endswith(filename):
                        return item
    return None


def get_current_playing_key(ps: PlexServer, target_client: PlexClient):
    info = get_current_playing_info(ps, target_client)
    return info[0] if info is not None else None


def get_current_playing_info(ps: PlexServer, target_client:PlexClient):
    """Return (session_key, offset_ms) for the current playing session on
    `target_client`, or ``None`` if no matching session is found.
    """
    if ps is None:
        return None
    for s in ps.sessions():
        if s.player.machineIdentifier == target_client.machineIdentifier:
            return (s.key, s.viewOffset)
    return None


async def _handle_play(target_client: PlexClient, filename: str, offset, ps: PlexServer):
    """Handle a play command: locate the media by filename and instruct the
    target client to play or resume.
    """
    # If Plex server is not available, log a warning
    if ps is None:
        logger.warning("Plex server unavailable while handling play for '%s'", filename)

    # Try to find media by filename
    media = await find_media_by_filename(ps, filename)

    # Normalize offset, default to 0 when not provided.
    if offset is None:
        offset = 0

    played = False
    if media is not None:
        played = True
        current_key = get_current_playing_key(ps, target_client)

        # If already playing the same media, just resume
        if current_key == media.key:
            logger.info("Resuming playback of %s on client %s", filename, target_client.machineIdentifier)
            target_client.play()
        # If a different media is playing, stop it then play the requested media
        elif current_key is not None:
            logger.info("Switching playback to %s on client %s. Offset: %s", filename, target_client.machineIdentifier, offset)
            target_client.stop()
            try:
                target_client.playMedia(media,offset)
            except Exception as e:
                logger.warning("Error during playMedia after stop: %s", e)
        # Otherwise, play the media immediately
        else:
            logger.info("Starting playback of %s on client %s. Offset: %s", filename, target_client.machineIdentifier, offset)
            try:
               target_client.playMedia(media,offset)
            except Exception as e:
                logger.warning("Error during playMedia: %s", e)
    global _current_playing_client, _current_playing_filekey, _current_playing_filename
    if played:
        _current_playing_client = target_client
        _current_playing_filekey = media.key
        _current_playing_filename = filename
    else:
        logger.warning("Requested media not found: %s", filename)
        clear_playing_globals()


async def _handle_pause(target_client: PlexClient, filename: str, ps: PlexServer):
    """Pause the target client if it is playing the expected filename."""
    # Verify that the target client is playing the same filename before pausing
    current_key = get_current_playing_key(ps, target_client)
    if filename == _current_playing_filename and current_key == _current_playing_filekey:
        target_client.pause()
    else:
        clear_playing_globals()


async def _handle_seek(target_client, filename: str, offset, ps: PlexServer):
    """Seek the target client to the specified offset (seconds) if it is
    playing the expected filename.
    """
    # Verify that the target client is playing the same filename before seeking
    current_key = get_current_playing_key(ps, target_client)
    if filename == _current_playing_filename and current_key == _current_playing_filekey:
        if offset is None:
            return
        logger.info("Seeking client %s to offset (s)=%s", getattr(target_client, 'machineIdentifier', None), offset/1000)
        target_client.seekTo(offset)


async def _handle_stop(target_client, filename: str, ps: PlexServer):
    """Stop the target client if it is playing the expected filename.

    Always clears local playing state after attempting the stop.
    """
    # Verify that the target client is playing the same filename before stopping
    current_key = get_current_playing_key(ps, target_client)
    if filename == _current_playing_filename and current_key == _current_playing_filekey:
        target_client.stop()
    clear_playing_globals()


async def status_loop(ws):
    while True:
        try:
            # Only send status updates if we have a known playing client and filename
            global _current_playing_client, _current_playing_filekey
            if not _current_playing_client or not _current_playing_filekey:
                await asyncio.sleep(60)
                continue

            # Verify the filename is still playing on the Plex client
            ps = get_plex_server()
            if ps is None:
                await asyncio.sleep(60)
                continue
            current_info = get_current_playing_info(ps, _current_playing_client)
          
            if current_info is not None and current_info[0] != _current_playing_filekey:
                # No longer playing the expected file; clear state to stop updates
                clear_playing_globals()
                await asyncio.sleep(60)
                continue

            # Send status update
            offset = current_info[1] if current_info is not None else 0
            payload = {"type": "status_update", "current_offset": offset, "filename": _current_playing_filename}
            logger.info("Sending status payload to server: %s", payload)
            await ws.send(json.dumps(payload))
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("Status loop error")
            await asyncio.sleep(1)


async def receive_loop(ws):
    """Continuously receive messages from the WebSocket and dispatch them."""
    async for msg in ws:
        try:
            data = json.loads(msg)
            await handle_command(data)
        except Exception:
            logger.warning("Error processing incoming message")

async def run_client():
    # validate and initialize settings at client startup
    global settings
    if settings is None:
        try:
            settings = config.ClientSettings()
        except Exception as e:
            raise RuntimeError(f"Failed to load client settings: {e}") from e

    # Do not log secrets (passkey, plex_token). Log non-sensitive config for debugging.
    logger.info("Loaded client settings: server_url=%s username=%s", settings.server_url, settings.username)

    # Ensure all required settings are present and non-empty
    required = ["server_url", "passkey", "username", "plex_url", "plex_token"]
    missing = []
    for key in required:
        val = getattr(settings, key, None)
        if val is None:
            missing.append(key)
        elif isinstance(val, str) and not val.strip():
            missing.append(key)
    if missing:
        logger.error("Missing required client settings: %s", ", ".join(missing))
        raise RuntimeError(f"Missing required client settings: {', '.join(missing)}")

    # build websocket URL with query params for passkey and username
    # Accept both http(s) and ws(s) server_url values by converting
    # http:// -> ws:// and https:// -> wss:// for the websocket connection.
    server_url_str = str(settings.server_url).rstrip('/')
    if server_url_str.startswith("http://"):
        ws_base = "ws://" + server_url_str[len("http://"):]
    elif server_url_str.startswith("https://"):
        ws_base = "wss://" + server_url_str[len("https://"):]
    else:
        ws_base = server_url_str

    url = f"{ws_base}/ws?username={settings.username}&passkey={settings.passkey}"
    logger.info("Connecting to server %s", settings.server_url)

    # Keep attempting to connect; on failure or disconnect wait 5s and retry.
    while True:
        try:
            async with websockets.connect(url) as ws:
                # Discover local Plex clients and populate cache
                fetch_plex_clients()

                # send registration with authorized clients so server can present options
                raw = settings.authorized_clients or ""
                authorized_clients = [c.strip() for c in raw.split(",") if c.strip()]

                # Log the configured authorized clients (raw and parsed)
                logger.info("Configured authorized_clients raw=%r parsed=%r", raw, authorized_clients)

                # only include validated devices; include both title and id
                auth_clients = []
                for c in authorized_clients:
                    client = get_plex_client_by_name(c)
                    if client is not None:
                        auth_clients.append({"title": client.title, "id": client.machineIdentifier})

                # Send server registration message
                register = {"type": "register", "authorized_clients": auth_clients}
                await ws.send(json.dumps(register))
                logger.info("Sent registration; authorized clients=%d payload=%s", len(auth_clients), register)

                # start status and receive loops
                recv_task = asyncio.create_task(receive_loop(ws))
                status_task = asyncio.create_task(status_loop(ws))
                done, pending = await asyncio.wait([recv_task, status_task], return_when=asyncio.FIRST_EXCEPTION)
                # If both tasks completed normally (e.g., in tests where they are short),
                # exit the client instead of reconnecting in a tight loop.
                if not pending:
                    return
                for t in pending:
                    t.cancel()
                # connection closed due to an exception in one of the tasks; will attempt reconnect
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("WebSocket connection failed or was closed during handshake")

        logger.info("Reconnecting to server in 5s...")
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            return


def main():
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run_client())
    except KeyboardInterrupt:
        logger.info("Client exiting")

if __name__ == "__main__":
    main()