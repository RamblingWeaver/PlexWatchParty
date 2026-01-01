from __future__ import annotations
from pydantic import BaseModel
from typing import Optional
from enum import Enum


class CommandType(str, Enum):
    PLAY = "play"
    PAUSE = "pause"
    SEEK = "seek"
    STOP = "stop"
    SYNC = "sync"


class WSCommand(BaseModel):
    type: CommandType
    # offset is an integer milliseconds reading from start of media
    offset: Optional[int] = None
    filename: str
    # device will carry a Device object when applicable and MUST be present
    device: "Device"


class Device(BaseModel):
    title: str
    id: str

__all__ = ["CommandType", "WSCommand", "Device"]
