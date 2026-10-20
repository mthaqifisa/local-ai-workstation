"""Wiring: which handler serves each job kind, and what resources each kind needs."""

from __future__ import annotations

from typing import Any

from law.handlers import core, creative, media, tasks
from law.handlers.base import Runtime
from law.queue.jobs import Job
from law.queue.scheduler import Capacity
from law.queue.worker import Handler

# Job kinds that need no model at all.
NO_MODEL = {"search", "song", "film", "film_join"}
# Job kinds whose model comes from the role in the payload (default role in brackets).
ROLE_KINDS = {"ask": "assistant", "draft": "assistant", "revise": "assistant", "judge": "reviewer",
              "task": "coder", "task_translate": "translator", "task_work": "coder", "task_review": "reviewer",
              "task_fix": "coder",
              "song_plan": "writer", "film_plan": "writer"}


def handlers(rt: Runtime) -> dict[str, Handler]:
    table: dict[str, Any] = {}
    table.update(core.make_handlers(rt))
    table.update(tasks.make_handlers(rt))
    table.update(media.make_handlers(rt))
    table.update(creative.make_handlers(rt))
    return table


def resolver(rt: Runtime) -> Any:
    def resolve(job: Job) -> Job:
        if job.kind in NO_MODEL:
            job.resource, job.lane, job.mem_gb = "", "none", 0.0
            return job
        if job.kind in ROLE_KINDS:
            role = job.payload.get("role", ROLE_KINDS[job.kind])
            if job.kind in {"judge", "task_review"}:
                role = "reviewer"
            elif job.kind == "task_translate":
                role = "translator"
            model = rt.reg.model(rt.model_id(role))
            job.resource, job.lane, job.mem_gb = f"llm:{model.id}", model.lane, model.mem_gb
            return job
        media = rt.reg.media.get(job.kind)
        if media is not None:
            job.resource, job.lane, job.mem_gb = f"{job.kind}:{media.model}", media.lane, media.mem_gb
            return job
        raise ValueError(f"unknown job kind {job.kind!r}")

    return resolve


def capacity(rt: Runtime) -> Capacity:
    return Capacity(budget_gb=float(rt.reg.machine.model_budget_gb))
