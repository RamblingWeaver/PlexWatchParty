#!/usr/bin/env python3
"""Interactive server tester.

Monkeypatches `server.api.verify_passkey` to accept any credentials and
runs the FastAPI app via uvicorn so a client can connect for interactive
testing from VS Code.

Usage: run this file directly. Optional args: --host, --port
"""
from __future__ import annotations
import argparse
import asyncio
import logging

import uvicorn

from server import api


def patch_verify():
    async def _accept_any(username: str, passkey: str) -> None:
        return None

    api.verify_passkey = _accept_any


async def _auto_start_sessions(poll_interval: float = 0.5) -> None:
    """Background watcher: when a new connected user has an authorized
    client device, create a session, add them as a participant, and start it.
    Runs until the process exits.
    """
    seen = set()
    while True:
        try:
            users = await api.ws_manager.list_users()
            for u in users:
                if u in seen:
                    continue
                # check whether the client has reported authorized clients
                devices = await api.ws_manager.get_authorized_clients(u)
                if not devices:
                    continue
                # pick first device
                client_name = devices[0].title

                # If there is an already-started session, add late joiners
                from datetime import datetime, timezone, timedelta
                inprogress_session = None
                try:
                    # inspect orchestrator sessions for an in-progress one
                    now_dt = datetime.now(timezone.utc)
                    for s in api.orchestrator._sessions.values():
                        st = getattr(s, "start_time", None)
                        if not st:
                            continue
                        # check that session hasn't exceeded its duration
                        elapsed_ms = int((now_dt - st).total_seconds() * 1000)
                        if elapsed_ms < getattr(s, "duration_ms", 0):
                            # choose the most recently started session
                            prev_st = getattr(inprogress_session, "start_time", None)
                            if inprogress_session is None or (prev_st is not None and st > prev_st) or prev_st is None:
                                inprogress_session = s
                except Exception:
                    inprogress_session = None

                if inprogress_session is not None:
                    try:
                        await api.orchestrator.add_participant(inprogress_session.session_id, u, client_name)
                        print(f"Added late-joiner {u} to in-progress session {inprogress_session.session_id}")
                        seen.add(u)
                        continue
                    except Exception:
                        # fall back to creating a new session if adding fails
                        pass

                # create session and add participant, then start shortly
                try:
                    from tests.test_config import FILENAME
                except Exception:
                    FILENAME = "interactive-test.mp4"

                # schedule the start a few seconds in the future so the
                # orchestrator's scheduled-start task will trigger.
                scheduled = datetime.now(timezone.utc) + timedelta(seconds=3)
                session = await api.orchestrator.create_session(
                    filename=FILENAME,
                    duration_ms=60 * 60 * 1000,
                    scheduled_start_time=scheduled,
                )
                try:
                    await api.orchestrator.add_participant(session.session_id, u, client_name)
                except Exception:
                    # if participant resolution failed, skip
                    pass
                try:
                    await api.orchestrator.start_session(session.session_id)
                    print(f"Started session {session.session_id} for user {u}")
                except Exception:
                    pass
                seen.add(u)
        except Exception:
            pass
        await asyncio.sleep(poll_interval)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    patch_verify()

    # If tests.test_config defines ADDRESS/PORT, use them when not explicitly passed
    if args.host is None or args.port is None:
        try:
            from tests.test_config import ADDRESS, PORT
            if args.host is None:
                args.host = ADDRESS
            if args.port is None:
                args.port = PORT
        except Exception:
            if args.host is None:
                args.host = "127.0.0.1"
            if args.port is None:
                args.port = 8001

    print(f"Starting interactive test server on http://{args.host}:{args.port}")
    print("WebSocket endpoint: /ws?username=<user>&passkey=<any>")

    config = uvicorn.Config(app=api.app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)

    async def _serve_and_watch():
        # start the server and the background watcher concurrently
        serve_task = asyncio.create_task(server.serve())
        watch_task = asyncio.create_task(_auto_start_sessions())
        await serve_task
        watch_task.cancel()

    try:
        asyncio.run(_serve_and_watch())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
