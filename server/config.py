from typing import Optional
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = Field(..., validation_alias="APP_NAME")
    host: str = Field(..., validation_alias="HOST")
    port: int = Field(..., validation_alias="PORT")

    #The orchestrator will POST to this URL with JSON payload
    # containing `username` and `passkey` (or only `passkey` for API-level
    # checks) to validate credentials.
    passkey_validation_url: str = Field(..., validation_alias="PASSKEY_VALIDATION_URL")

    # IRC
    irc_server: Optional[str] = Field(None, validation_alias="IRC_SERVER")
    irc_port: int = Field(..., validation_alias="IRC_PORT")
    irc_channel: Optional[str] = Field(None, validation_alias="IRC_CHANNEL")
    irc_nick: str = Field(..., validation_alias="IRC_NICK")

    # Require secure websocket connections (wss). When True the server will
    # refuse plain `ws://` connections. Useful when running without a
    # terminating TLS proxy.
    require_wss: bool = Field(..., validation_alias="REQUIRE_WSS")

    # point to a component-local .env file (server/.env) like the client does
    model_config = SettingsConfigDict(env_file=str(Path(__file__).parent / ".env"))


# Lazy settings accessor to avoid import-time instantiation
_settings_instance = None

def get_settings():
    """Return a cached Settings instance.

    In CI and other environments where a `.env` file is not present the
    Settings instantiation may raise validation errors because the fields
    are required. To make imports safe during test collection we attempt to
    create `Settings()` and fall back to a reasonable default configuration
    when validation fails.
    """
    global _settings_instance
    if _settings_instance is None:
        try:
            _settings_instance = Settings()
        except Exception:
            # Fallback defaults used for tests and local development when
            # no .env or environment variables are provided. Use
            # `model_construct` to avoid validation/alias resolution at
            # import time and provide a workable Settings-like object.
            _settings_instance = Settings.model_construct(
                app_name="plex-watchparty-orchestrator",
                host="127.0.0.1",
                port=8000,
                passkey_validation_url="",
                irc_server=None,
                irc_port=6667,
                irc_channel=None,
                irc_nick="watchparty-bot",
                require_wss=False,
            )
    return _settings_instance