from typing import Optional

from pydantic import Field, AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "plex-watchparty-orchestrator"
    host: str = "0.0.0.0"
    port: int = 8000
    # Optional external validation endpoint for username/passkey pairs.
    # If set, the orchestrator will POST to this URL with JSON payload
    # containing `username` and `passkey` (or only `passkey` for API-level
    # checks) to validate credentials.
    passkey_validation_url: Optional[str] = Field(None, validation_alias="PASSKEY_VALIDATION_URL")

    # IRC
    irc_server: Optional[str] = Field(None, validation_alias="IRC_SERVER")
    irc_port: int = Field(6667, validation_alias="IRC_PORT")
    irc_channel: Optional[str] = Field(None, validation_alias="IRC_CHANNEL")
    irc_nick: str = Field("watchparty-bot", validation_alias="IRC_NICK")


try:
    # Prefer normal instantiation which will load from the env file.
    settings = Settings()
except TypeError:
    # Some static analyzers (Pylance) may report the BaseSettings __init__
    # signature as requiring parameters. Using `model_validate` avoids that
    # while producing an equivalent Settings instance at runtime.
    settings = Settings.model_validate({})
