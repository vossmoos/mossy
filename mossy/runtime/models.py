"""Task and envelope models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum, IntEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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
    not_before: datetime | None = None
    result: dict[str, Any] | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    error: str | None = None

    @field_validator("created_at", "not_before")
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        return normalize_utc(value)


class Envelope(BaseModel):
    payload: str
    priority: int | None = None
    scheduled_for: datetime | None = None
    source: str = "http"
    task_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("scheduled_for")
    @classmethod
    def normalize_scheduled_for(cls, value: datetime | None) -> datetime | None:
        return normalize_utc(value)
