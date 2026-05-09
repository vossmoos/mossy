"""Task and envelope models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum, IntEnum
from typing import Any

from pydantic import BaseModel, Field


class Priority(IntEnum):
    INTERRUPT = 0
    USER_INPUT = 1
    AUTONOMOUS = 2
    BACKGROUND = 3
    IDLE = 4


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(BaseModel):
    id: str
    goal: str
    priority: int = Priority.AUTONOMOUS
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    error: str | None = None


class Envelope(BaseModel):
    payload: str
    priority: int | None = None
    source: str = "http"
    task_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
