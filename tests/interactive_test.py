#!/usr/bin/env python3
"""Interactive runner to call `handle_command()` from terminal.

This runner uses the real Plex client configuration from `client/.env`.
"""
import argparse
import asyncio
import json
import sys
import os
import logging
from types import SimpleNamespace


# Ensure project root is on sys.path so `server` package imports work when running
# this script directly from the `tests` directory.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from client import client as client_mod
    import importlib
    config_mod = importlib.import_module("client.config")
    # try loading test config from either the client package or the tests package
    test_config = None
    for _m in ("client.test_config", "tests.test_config"):
        try:
            test_config = importlib.import_module(_m)
            break
        except ModuleNotFoundError:
            pass
except ModuleNotFoundError as e:
    print("Dependency error when importing project modules:", e)
    print("Run this script using the project's virtualenv python, e.g:")
    print("  /root/server/venv/bin/python tests/interactive_test.py --mock")
    sys.exit(1)

# Uses real Plex server and clients configured via `client/.env`


async def run_command(obj):
    # Do not inject playback state from tests; let client logic maintain state
    await client_mod.handle_command(obj)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--connect", action="store_true", help="Start the real client and connect to server")
    args = parser.parse_args()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    if args.connect:
        # If test config defines a device, set `authorized_clients` so the
        # client will include it in its registration using the standard flow
        # (discovery/authorized_clients), while still using real Plex settings.

        # Ensure we use the local interactive server as server_url while
        # preserving real Plex settings from client config/.env so the
        # client connects to a real Plex server.
        try:
            # load real settings from ClientSettings (reads client/.env)
            real_settings = config_mod.ClientSettings()
            # override server_url to point at our interactive server when configured
            try:
                from tests.test_config import ADDRESS, PORT
                real_settings.server_url = f"http://{ADDRESS}:{PORT}"
            except Exception:
                pass
            client_mod.settings = real_settings
        except Exception as e:
            print("Failed to load client settings from .env:", e)
            # proceed; run_client will error if required settings missing

        # Ensure logging is configured so client logger.info messages are visible
        logging.basicConfig(level=logging.INFO)

        # Start the client websocket connection (uses client_mod.settings)
        try:
            loop.run_until_complete(client_mod.run_client())
        except Exception as e:
            print("Client connect failed:", e)
        # no test hook to clean up; using standard `authorized_clients`
        return

    print("Interactive REPL (enter JSON commands, 'exit' to quit)")
    try:
        while True:
            line = input("> ").strip()
            if not line:
                continue
            if line in ("exit", "quit"):
                break
            try:
                obj = json.loads(line)
            except Exception as e:
                print("Invalid JSON:", e)
                continue
            loop.run_until_complete(run_command(obj))
    except (KeyboardInterrupt, EOFError):
        pass


if __name__ == "__main__":
    main()
