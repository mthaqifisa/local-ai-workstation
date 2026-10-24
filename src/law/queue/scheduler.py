"""Pure scheduling policy: which queued jobs start now. No I/O, so it is exhaustively testable.

Policy (ADR-0009):
1. Effective priority = class - floor(wait / aging_s), floored at 0 (starvation-free).
2. Heavy lane: one job at a time. Within the best effective class prefer the resident model
   (avoids a swap), then the model with the most queued work (batches swaps), then FIFO.
3. Light lane: run concurrently while slots and the memory budget allow.
4. Jobs with lane "none" (pure I/O, e.g. web search) start immediately up to their slot limit.
5. Shared heavy slots (ADR-0017): while a heavy job runs, a job marked `share_model` (a chat answer) starts beside
   it on the model already loaded; the model server batches both. A chat never waits hours behind an agent step,
   and no second model is loaded.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from law.queue.jobs import Job


@dataclass(frozen=True)
class Capacity:
    budget_gb: float
    light_slots: int = 3
    none_slots: int = 8
    shared_slots: int = 2
    aging_s: float = 300.0


@dataclass
class Running:
    heavy: Job | None = None
    shared: list[Job] = field(default_factory=list)  # running beside `heavy`, on its model
    light: list[Job] = field(default_factory=list)
    none: list[Job] = field(default_factory=list)

    def used_gb(self) -> float:
        return (self.heavy.mem_gb if self.heavy else 0.0) + sum(j.mem_gb for j in self.light)


def effective_class(job: Job, now: float, aging_s: float) -> int:
    return max(0, int(job.priority) - math.floor(job.wait_seconds(now) / aging_s))


def can_share(job: Job) -> bool:
    """A job that may run on whichever heavy model is loaded (see rule 5)."""
    return job.lane == "heavy" and bool(job.payload.get("share_model"))


def pick(queued: list[Job], running: Running, resident: str | None, cap: Capacity, now: float) -> list[Job]:
    """Return the jobs to start now, in start order."""
    chosen: list[Job] = []
    used = running.used_gb()
    pending = [j for j in queued if not j.cancel_requested]

    # Lane "none": no model, no memory — only a concurrency cap.
    free_none = cap.none_slots - len(running.none)
    for job in sorted((j for j in pending if j.lane == "none"),
                      key=lambda j: (effective_class(j, now, cap.aging_s), j.enqueued)):
        if free_none <= 0:
            break
        chosen.append(job)
        free_none -= 1

    # Heavy lane: at most one.
    if running.heavy is None:
        heavy = [j for j in pending if j.lane == "heavy"]
        if heavy:
            demand = Counter(j.resource for j in heavy)

            def key(j: Job) -> tuple[int, int, int, float]:
                return (effective_class(j, now, cap.aging_s), 0 if j.resource == resident else 1,
                        -demand[j.resource], j.enqueued)

            best = min(heavy, key=key)
            light_gb = sum(j.mem_gb for j in running.light)
            if best.mem_gb + light_gb <= cap.budget_gb:
                chosen.append(best)
                used += best.mem_gb
    elif running.heavy.resource.startswith("llm:"):
        # Shared heavy slots: chat answers join the running language model (needs no extra weights). Never a
        # media model: a chat cannot run on an image or video model.
        free_shared = cap.shared_slots - len(running.shared)
        for job in sorted((j for j in pending if can_share(j)),
                          key=lambda j: (effective_class(j, now, cap.aging_s), j.enqueued)):
            if free_shared <= 0:
                break
            chosen.append(job)
            free_shared -= 1

    # Light lane: concurrent while memory allows. Light jobs never block a waiting heavy job
    # from fitting: they only take memory the best heavy candidate would leave free.
    free_light = cap.light_slots - len(running.light)
    reserve = 0.0
    if running.heavy is None and not any(j.lane == "heavy" for j in chosen):
        waiting_heavy = [j for j in pending if j.lane == "heavy"]
        reserve = min((j.mem_gb for j in waiting_heavy), default=0.0)
    for job in sorted((j for j in pending if j.lane == "light"),
                      key=lambda j: (effective_class(j, now, cap.aging_s), j.enqueued)):
        if free_light <= 0:
            break
        if used + job.mem_gb + reserve > cap.budget_gb:
            continue
        chosen.append(job)
        used += job.mem_gb
        free_light -= 1
    return chosen
