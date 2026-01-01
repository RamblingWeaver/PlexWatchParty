from pydantic import Field, AnyUrl
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
from pathlib import Path


class ClientSettings(BaseSettings):
    # Required client settings: validated at runtime in client startup
    server_url: AnyUrl = Field(..., validation_alias="ORC_SERVER_URL")
    passkey: str = Field(..., validation_alias="ORC_PASSKEY")
    username: str = Field(..., validation_alias="ORC_USERNAME")
    # comma-separated list of authorized local Plex clients names
    authorized_clients: Optional[str] = Field(None, validation_alias="ORC_AUTHORIZED_CLIENTS")
    # Plex local server credentials (kept local to the client machine)
    plex_url: str = Field(..., validation_alias="PLEX_URL")
    plex_token: str = Field(..., validation_alias="PLEX_TOKEN")

    model_config = SettingsConfigDict(env_file=str(Path(__file__).parent / ".env"))


# Do not instantiate settings at import time; the client will validate and
# create a `ClientSettings` instance at startup. Tests should set `client_module.settings`
# directly when needed.
