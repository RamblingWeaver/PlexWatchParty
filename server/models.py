"""Server models module.

Shared models (used by client and server) are imported from
`shared.models`. Server-only models remain defined here.
"""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import List, Dict, Optional
import asyncio
from datetime import datetime

from shared.models import CommandType, WSCommand, Device


class PauseInterval(BaseModel):
    start: datetime
    end: Optional[datetime]


class Participant(BaseModel):
    username: str
    device: Device
    join_time: datetime
    offset: int = 0


class ClientStatus(BaseModel):
    username: str
    filename: str
    # offset in milliseconds from start of media
    current_offset: int = 0
    last_update_time: datetime


class Session(BaseModel):
    session_id: str
    filename: str
    # total media duration in milliseconds
    duration_ms: int
    # scheduled start time (required) and actual start_time (set when started)
    scheduled_start_time: datetime
    start_time: Optional[datetime]
    pause_intervals: List[PauseInterval] = Field(default_factory=list)
    participants: Dict[str, Participant] = Field(default_factory=dict)
    prequeue: List[str] = Field(default_factory=list)
    catchup_queue: List[str] = Field(default_factory=list)
    # runtime-only per-session asyncio.Task handles (not serialized)
    start_task: Optional[asyncio.Task] = Field(default=None, exclude=True)
    pause_task: Optional[asyncio.Task] = Field(default=None, exclude=True)
    resume_task: Optional[asyncio.Task] = Field(default=None, exclude=True)

    # Allow asyncio.Task to be used in runtime-only fields without pydantic schema errors
    model_config = {"arbitrary_types_allowed": True}


__all__ = [
    "CommandType",
    "PauseInterval",
    "Participant",
    "ClientStatus",
    "Session",
    "WSCommand",
    "Device",
]
