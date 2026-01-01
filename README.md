# Plex Watch Party
Plex Watch Party enables people to watch the same Plex media together from different locations. The repository contains the pieces needed to coordinate a synchronized viewing experience: a server that manages sessions and timing, and small client agents that run on users' machines and control local Plex players. It focuses on reliable synchronization, simple authorization, and developer-friendly tooling for local testing.

**Who this is for**
- Friends or groups who want synchronized playback using their own Plex clients.
- Developers building integration viewing around Plex playback coordination.

**Key capabilities (high-level)**
- Create and schedule watch-party sessions.
- Keep everyone in sync (PLAY/PAUSE/SEEK/STOP) and correct drift automatically.
- Add late joiners and bring them up to the current play position.
- Schedule a brief (10-minute) pause after the first hour of playback. Additional intermissions occur hourly only if more than 30 minutes of playback remain after the pause.
- Passkey-based client authorization with external validation.

Read below for the technical details (client/server deepdive and running instructions).

**Client Overview**
- **Purpose:** The client is a lightweight WebSocket client that connects to the orchestrator, registers available local Plex devices, reports playback status, and receives playback commands from the server.
- **Core features:**
	- **Register:** on connect the client sends a `register` message with `authorized_clients` so the server can present device options to session creators.
	- **Status updates:** periodic `status_update` messages report the current filename and offset so the server can compute the canonical session offset and detect drift.
	- **Command handling:** supports `PLAY`, `PAUSE`, `SEEK`, and `STOP` commands sent from the server. Each command includes the target `device` so the client knows which local Plex client to control.
	- **Reconnect behavior:** the client will retry the WebSocket connection if the server is not yet available or goes down.

**Configuration (.env)**
- Client `.env` (location: `client/.env` — loaded by `client/config.py` via `ClientSettings`):
	- `ORC_SERVER_URL` — orchestrator base URL (e.g. `http://127.0.0.1:8000`). The client converts `http(s)` to `ws(s)` for WebSocket.
	- `ORC_PASSKEY` — passkey used to authenticate to the orchestrator.
	- `ORC_USERNAME` — username presented to the orchestrator.
	- `ORC_AUTHORIZED_CLIENTS` — comma-separated list of local Plex client titles to include in registration (discovery will validate).
	- `PLEX_URL` — URL of the local Plex Media Server (e.g. `http://localhost:32400`).
	- `PLEX_TOKEN` — Plex token for local server access.
 
**Server Overview**
- **Purpose:** The server (orchestrator) manages watch-party sessions, schedules starts/pauses/resumes, tracks canonical offsets, and dispatches playback commands to connected clients via WebSocket.
- **Core responsibilities:**
	- **Session lifecycle:** create sessions, maintain prequeue, set `start_time`, schedule automatic pauses/resumes, and stop sessions when complete.
	- **Authorization:** verify connecting clients via `PASSKEY_VALIDATION_URL` and manage per-user `authorized_clients` lists.
	- **Command dispatch:** build and broadcast per-user `WSCommand` objects (PLAY/PAUSE/SEEK/STOP) including each recipient's chosen `Device` so clients can target the correct local Plex client.
	- **Drift correction:** compute server offsets from session `start_time` and pause intervals, compare client-reported offsets, and issue SEEK commands when drift exceeds a threshold.
	- **WebSocket management:** accept client connections at `/ws`, persist last-known client status, and provide helper APIs to list users and authorized devices.

**Server configuration (.env)**
- `PASSKEY_VALIDATION_URL` — POSTs `{"username":...,"passkey":...}` to validate credentials; if unset the interactive tester allows any passkey for local testing.
- `IRC_SERVER`, `IRC_PORT`, `IRC_CHANNEL`, `IRC_NICK` — optional IRC integration settings for announcements; not required for basic operation.
- `host`, `port`, `app_name` — orchestrator bind host/port and application naming set in `server/config.py`.

**APIs & operations**
- WebSocket endpoint: `/ws?username=<user>&passkey=<passkey>` — clients must include both query params on connect.
- Orchestrator methods (programmatic): `create_session(filename, duration_ms, scheduled_start_time)`, `add_participant(session_id, username, device_title)`, `start_session(session_id)`, `pause_session(session_id)`, `resume_session(session_id)`.

**Logs & observability**
- Important orchestrator log events:
	- `Creating session <id>` when a session is created.
	- `Scheduled start task for session <id>` when a scheduled start is registered.
	- `Added participant <user> to session <id>` when users are added.
	- `Session <id> start_time set to ...; prequeue=[...]` when a session starts and prequeue is captured.
	- Broadcast and send logs showing per-user commands being dispatched.

**Playback commands**
- `PLAY`: start or resume playback on the specified device. Includes `filename` and `offset` (milliseconds) to align playback.
- `PAUSE`: pause playback on the specified device.
- `SEEK`: jump to a specified offset (milliseconds) on the specified device.
- `STOP`: stop playback and clear local play state.

**Example payloads**
- **Register**
```json
{
	"type": "register",
	"username": "alice",
	"authorized_clients": [ { "title": "Living Room TV", "id": "device-id" } ]
}
```
- **Playback command** (PLAY/PAUSE/SEEK/STOP): see the "Playback commands" section above for field meanings. Example:
```json
{
	"type": "command",
	"command": "PLAY",
	"device": { "title": "Living Room TV", "id": "device-id" } ,
	"filename": "Movie.mkv",
	"offset": 123456
}
```
- **Status update**
```json
{
	"type": "status_update",
	"filename": "Movie.mkv",
	"offset": 123456,
}
```

**Interactive testing (VS Code launch)**
- **What it does:** A local-only test that runs both the orchestrator and a client in the same environment for rapid validation. The server accepts any credentials and automatically starts a session using the file defined in test_config.py upon first client connect. Subsequent late-joining clients are automatically attached to an active in-progress session.
- **How to run (VS Code):** use the compound configuration in `.vscode/launch.json` (select the "Server + Client" compound) to start the server and client together from the VS Code Run/Debug pane.
- **Vars to define:**
	- ORC_AUTHORIZED_CLIENTS, PLEX_URL, and PLEX_TOKEN are defined in client/.env.
	- FILENAME, ADDRESS, abd PORT are defined in tests/test_config.py.
- **Runtime behavior:**
	- The server runs on ADDRESS and PORT. The client connects to the orchestration server using ADDRESS and PORT, then connects to the Plex server using PLEX_URL and authenticates with PLEX_TOKEN.

	- After establishing the Plex connection, the client validates the active clients against ORC_AUTHORIZED_CLIENTS and reports the authorized clients to the server. The server then starts a session using FILENAME and sends the initial play commands to the first Plex device.

**Run Instructions**
- **Prerequisites:** create and activate a Python virtualenv and install dependencies from `requirements.txt`:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
- **Server (orchestrator)**:
	- **Env:** set `PASSKEY_VALIDATION_URL` (or leave unset for local testing), and other optional vars in `server/.env` or your environment.
	- **Run:** from the repository root run:
```bash
# run the FastAPI server using uvicorn
python3 -m server.main
```
- **Client**:
	- **Env:** configure `client/.env` with `ORC_SERVER_URL`, `ORC_PASSKEY`, `ORC_USERNAME`, `ORC_AUTHORIZED_CLIENTS`, `PLEX_URL`, and `PLEX_TOKEN`.
	- **Run:** from the repository root run:

```bash
# run the client which connects to the orchestrator and registers local Plex clients
python -m client.client
```
- **Notes:**
	- The client and server communicate over WebSocket at `/ws`. Provide `username` and `passkey` query parameters when connecting.
	- The `requirements.txt` contains the libraries used by both server and client; if you add runtime dependencies, update that file.

**TODO:**
- Setup docker images for client and server for easier deployment.
- IRC bot is largely place holder code and needs to be implemented.