"""Job records shared by the API, CLI, Telegram adapter and worker."""

from __future__ import annotations

import time
import uuid
from enum import IntEnum, StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Priority(IntEnum):
    """Lower runs first. Waiting jobs age toward INTERACTIVE so nothing starves."""

    INTERACTIVE = 0  # a person is waiting (Telegram, CLI)
    NORMAL = 1       # agent sub-tasks, Open WebUI tool calls
    BATCH = 2        # background work (indexing, nightly save, evals)


class Status(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"      # pipeline root waiting for its stages
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD = "dead"            # exhausted retries

    @property
    def terminal(self) -> bool:
        return self in {Status.DONE, Status.FAILED, Status.CANCELLED, Status.DEAD}


Lane = Literal["heavy", "light", "none"]


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Job(BaseModel):
    id: str = Field(default_factory=new_id)
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: Priority = Priority.INTERACTIVE
    source: str = "api"
    # Resource the job needs; the scheduler uses these for affinity and memory admission.
    resource: str = ""          # e.g. "llm:qwen3.8-27b-8bit", "mflux:qwen-image", "" (no model)
    lane: Lane = "none"
    mem_gb: float = 0.0
    status: Status = Status.QUEUED
    attempts: int = 0
    max_attempts: int = 3
    created: float = Field(default_factory=time.time)
    enqueued: float = Field(default_factory=time.time)  # when it (re)entered the queue
    started: float | None = None
    finished: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    parent: str | None = None   # pipeline root id
    stage: int = 0
    reply_to: dict[str, Any] | None = None  # e.g. {"telegram_chat": 123, "message": 45}
    cancel_requested: bool = False

    def wait_seconds(self, now: float) -> float:
        return max(0.0, now - self.enqueued)
